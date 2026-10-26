"""Finite, synchronized demo_v2 workflow_run HTTP load test (Python 3.10+)."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import statistics
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

HOST = 'https://gaia-prj-03-0042-v2-api-basic-dev.api.hcpnd01.skhynix.com'
SCENARIO_PATH = Path(__file__).resolve().parents[2] / 'app/agents/orchestration/mock_workflow_scenario.json'
SCENARIO = json.loads(SCENARIO_PATH.read_text(encoding='utf-8'))
TOOL_VALUES = {'data_type': 'nce', 'lot_cd': 'LOT1', 'process': ['P01'],
               'start_dt': '2026-01-01', 'end_dt': '2026-02-01', 'query_mode': 'period'}


def decision(interaction, values):
    if interaction.get('payload_version') != 2:
        raise ValueError('Expected HITL payload_version=2')
    action = interaction['action_requests'][0]
    args = action.get('args') or {}
    name = action['name']
    if name == 'workflow_candidate_selection':
        return 'edit', {'intent': 'select', 'candidate_id': args['candidates'][0]['candidate_id']}
    if name == 'workflow_approval':
        return 'approve', None
    if name != 'additional_information':
        raise ValueError(f'Unsupported interaction: {name}')
    expected = {(f['step_index'], f['tool_index'], f['parameter']): f for f in SCENARIO['fields']}
    actual = {(t['step_index'], t['tool_index'], p['name'])
              for t in args.get('tool_inputs', []) for p in t.get('parameters', [])}
    if actual != set(expected):
        raise ValueError(f"Fixed mock form changed: missing={sorted(set(expected)-actual)}, extra={sorted(actual-set(expected))}")
    inputs = []
    for tool in args.get('tool_inputs', []):
        for param in tool.get('parameters', []):
            if not param.get('editable'):
                continue
            address = {k: tool[k] for k in ('step_index', 'tool_index')}
            address['parameter'] = param['name']
            fixed = expected[(tool['step_index'], tool['tool_index'], param['name'])]
            binding = fixed['argument']
            if binding['source'] == 'tool_output':
                ref = {k: binding[k] for k in ('step_index', 'tool_index', 'output')}
                offered = [{k: o[k] for k in ref} for o in param.get('output_options', [])]
                if ref not in offered:
                    current = param.get('binding') or {}
                    if all(current.get(k) == v for k, v in ref.items()):
                        # Existing non-data references are preserved by omission;
                        # the UI only offers rewiring for compatible data outputs.
                        continue
                    raise ValueError(f'Fixed mock output reference unavailable: {address}: {ref}')
                inputs.append(dict(address, source='tool_output', reference=ref))
                continue
            value = values.get(param['name'], fixed['value']) if binding['source'] == 'input' else fixed['value']
            inputs.append(dict(address, source='value', value=value))
    return 'edit', dict(intent='submit_inputs', workflow_id=args['workflow_id'],
                        workflow_revision=args['workflow_revision'], inputs=inputs)


def executor_waiting(run):
    return any(i.get('kind') == 'EXECUTOR_EVENT' for i in run.get('interrupt') or [])


def validate_metrics(run):
    queue = run.get('queue_wait_seconds')
    if not isinstance(queue, (int, float)) or not math.isfinite(queue) or queue < 0:
        raise ValueError('Missing/invalid server queue_wait_seconds (not treated as zero)')
    nodes = run.get('node_timings')
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError('Missing server node_timings')
    for name, bucket in nodes.items():
        if not isinstance(bucket, dict):
            raise ValueError(f'Invalid node timing: {name}')
        for key in ('wall_seconds', 'min_seconds', 'max_seconds', 'count'):
            value = bucket.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid {name}.{key}')
        if bucket['count'] <= 0:
            raise ValueError(f'Invalid {name}.count')


class Client:
    def __init__(self, args):
        self.args = args
        self.base = args.host.rstrip('/') + '/' + args.api_prefix.strip('/')
        self.headers = {'Content-Type': 'application/json', 'X-DTest-Mock-LLM': '1',
                        'X-DTest-Mock-LLM-Sleep': str(args.mock_sleep)}
        self.requests = []

    def call(self, method, path, body=None, deadline=None):
        remaining = self.args.request_timeout if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Workflow deadline exceeded')
        headers = dict(self.headers)
        if method == 'POST':
            headers['Idempotency-Key'] = uuid4().hex
        request = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        started = time.monotonic()
        status, error = None, None
        try:
            with urllib.request.urlopen(request, timeout=min(remaining, self.args.request_timeout)) as response:
                status = response.status
                return json.load(response)
        except urllib.error.HTTPError as exc:
            status = exc.code
            error = exc.read(1000).decode(errors='replace')
            raise RuntimeError(f'{method} {path}: HTTP {status}: {error}') from exc
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            self.requests.append(dict(method=method, path=path, status=status,
                elapsed_seconds=time.monotonic() - started, error=error))


def cycle(args, users, round_number, user_number, barrier):
    client = Client(args)
    result = dict(users=users, round=round_number, user=user_number, user_id=None,
                  task_id=None, runs=[], requests=client.requests, error=None, elapsed_seconds=None)
    started = None
    try:
        account = client.call('POST', '/users', {'user_name': 'load-v2-' + uuid4().hex})
        result['user_id'] = account['user_id']
        client.headers['X-User-ID'] = str(account['user_id'])
        projects = client.call('GET', '/projects')['items']
        project = next((p for p in projects if p.get('is_default')), projects[0])
        barrier.wait(timeout=args.setup_timeout)
        started = time.monotonic()
        deadline = started + args.flow_timeout
        body = {'input': {'content': [{'type': 'text', 'text': args.prompt}]},
                'project_id': project['id']}
        expected_actions = ['workflow_candidate_selection', 'additional_information', 'workflow_approval']
        completed_actions = []
        for step in range(args.max_steps + 1):
            snapshot = client.call('POST', '/workflow_run', body, deadline)
            result['task_id'] = snapshot['task_id']
            run_id = snapshot['submission_run_id']
            latest = {'id': run_id}
            result['runs'].append(latest)
            metrics_deadline = None
            while True:
                run = client.call('GET', f'/workflow_run/{run_id}', deadline=deadline)
                latest.clear()
                latest.update(run)
                status = run['run_status']
                if status in ('error', 'timeout', 'canceled'):
                    raise RuntimeError(f'Run {run_id}: {status}: {run.get("failure")}')
                ready = status in ('interrupted', 'success') or executor_waiting(run)
                if ready:
                    # Metrics and status may become visible in adjacent commits.
                    try:
                        validate_metrics(run)
                        break
                    except ValueError:
                        if metrics_deadline is None:
                            metrics_deadline = min(deadline, time.monotonic() + 5)
                        if time.monotonic() + args.poll_interval >= metrics_deadline:
                            raise
                elif status not in ('pending', 'running'):
                    raise RuntimeError(f'Unknown run status: {status}')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Workflow deadline exceeded')
                time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))
            if status == 'success' or executor_waiting(run):
                if completed_actions != expected_actions:
                    raise ValueError(f'Workflow ended before fixed scenario completed: {completed_actions}')
                break
            interactions = [i for i in run.get('interactions') or []
                            if (i.get('action_requests') or [{}])[0].get('name') != 'next_user_request']
            if len(interactions) != 1:
                raise ValueError(f'Expected one open interaction, got {len(interactions)}')
            interaction = interactions[0]
            action_name = interaction['action_requests'][0]['name']
            if len(completed_actions) >= len(expected_actions) or action_name != expected_actions[len(completed_actions)]:
                raise ValueError(f'Unexpected fixed scenario interaction: {action_name}')
            action, payload = decision(interaction, args.tool_values)
            completed_actions.append(action_name)
            resume = dict(action=action, interaction_id=interaction['interaction_id'], revision=interaction['revision'])
            if payload is not None:
                resume['payload'] = payload
            body = dict(task_id=result['task_id'], run_id=run_id, command={'resume': resume})
        else:
            raise RuntimeError('Too many HITL steps')
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        if started is None:
            barrier.abort()
    finally:
        if started is not None:
            result['elapsed_seconds'] = time.monotonic() - started
    return result


def percentile(values, percent):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * percent / 100) - 1)]


def write_csv(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def report(out, samples):
    queues, nodes, summaries = [], [], []
    for sample in samples:
        identity = {k: sample[k] for k in ('users', 'round', 'user', 'task_id')}
        for run in sample['runs']:
            key = dict(identity, run_id=run.get('id'), run_status=run.get('run_status'))
            queues.append(dict(key, queue_wait_seconds=run.get('queue_wait_seconds'),
                               created_at=run.get('created_at'), started_at=run.get('started_at'),
                               error=sample['error']))
            for name, timing in (run.get('node_timings') or {}).items():
                nodes.append(dict(key, node=name, **{k: timing.get(k) for k in
                    ('wall_seconds', 'count', 'min_seconds', 'max_seconds')}, error=sample['error']))
    for users, round_number in sorted({(s['users'], s['round']) for s in samples}):
        group = [s for s in samples if (s['users'], s['round']) == (users, round_number)]
        good = [s for s in group if s['error'] is None]
        times = [s['elapsed_seconds'] for s in good]
        waits = [r['queue_wait_seconds'] for s in good for r in s['runs']]
        summaries.append(dict(users=users, round=round_number, requested=users,
            success=len(good), failed=len(group)-len(good),
            flow_avg_s=statistics.mean(times) if times else None, flow_p95_s=percentile(times, 95),
            queue_avg_s=statistics.mean(waits) if waits else None, queue_p95_s=percentile(waits, 95)))
    write_csv(out / 'summary.csv', summaries, ['users', 'round', 'requested', 'success', 'failed',
        'flow_avg_s', 'flow_p95_s', 'queue_avg_s', 'queue_p95_s'])
    keys = ['users', 'round', 'user', 'task_id', 'run_id', 'run_status']
    write_csv(out / 'queue_wait.csv', queues, keys + ['queue_wait_seconds', 'created_at', 'started_at', 'error'])
    write_csv(out / 'node_timings.csv', nodes, keys + ['node', 'wall_seconds', 'count', 'min_seconds', 'max_seconds', 'error'])


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--api-prefix', default='/api/v1')
    parser.add_argument('--users', type=int, nargs='+', default=[1, 5, 10, 20, 50, 100, 150])
    parser.add_argument('--rounds', type=int, choices=range(1, 6), default=5)
    parser.add_argument('--prompt', default=SCENARIO['prompt'])
    parser.add_argument('--mock-sleep', type=float, default=0)
    parser.add_argument('--poll-interval', type=positive, default=0.5)
    parser.add_argument('--request-timeout', type=positive, default=60)
    parser.add_argument('--flow-timeout', type=positive, default=900)
    parser.add_argument('--setup-timeout', type=positive, default=180)
    parser.add_argument('--max-steps', type=int, default=20)
    parser.add_argument('--tool-values', type=Path, help='JSON object overriding fixed tool values')
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--dry-run', action='store_true', help='Print matrix without HTTP requests')
    args = parser.parse_args(argv)
    if args.prompt != SCENARIO['prompt']:
        parser.error('This fixed scenario requires the prompt: ' + SCENARIO['prompt'])
    if any(n < 1 or n > 150 for n in args.users) or args.max_steps < 1:
        parser.error('users must be 1..150 and max-steps must be positive')
    if args.mock_sleep < 0 or not math.isfinite(args.mock_sleep):
        parser.error('mock-sleep must be finite and nonnegative')
    args.users = list(dict.fromkeys(args.users))
    overrides = json.loads(args.tool_values.read_text(encoding='utf-8-sig')) if args.tool_values else {}
    if not isinstance(overrides, dict):
        parser.error('tool-values must contain a JSON object')
    args.tool_values = dict(TOOL_VALUES, **overrides)
    print(f'Host: {args.host}; users: {args.users}; rounds: {args.rounds}; workflows: {sum(args.users)*args.rounds}', flush=True)
    if args.dry_run:
        return 0
    out = args.out_dir or Path('var') / ('workflow-load-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6])
    out.mkdir(parents=True, exist_ok=False)
    (out / 'config.json').write_text(json.dumps(vars(args), default=str, ensure_ascii=False, indent=2), encoding='utf-8')
    samples = []
    with (out / 'samples.jsonl').open('w', encoding='utf-8') as raw:
        for users in args.users:
            for round_number in range(1, args.rounds + 1):
                barrier = threading.Barrier(users)
                with concurrent.futures.ThreadPoolExecutor(max_workers=users) as pool:
                    futures = [pool.submit(cycle, args, users, round_number, i+1, barrier) for i in range(users)]
                    for future in concurrent.futures.as_completed(futures):
                        sample = future.result()
                        samples.append(sample)
                        raw.write(json.dumps(sample, ensure_ascii=False) + '\n')
                        raw.flush()
                report(out, samples)
                failures = sum(s['error'] is not None for s in samples if s['users'] == users and s['round'] == round_number)
                print(f'users={users} round={round_number}: success={users-failures} failed={failures}', flush=True)
    print(f'Reports: {out.resolve()}', flush=True)
    return int(any(s['error'] is not None for s in samples))


if __name__ == '__main__':
    raise SystemExit(main())
