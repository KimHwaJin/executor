"""Offline HTTP contract tests; never contact the deployment."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import run_workflow_load as load


class Handler(BaseHTTPRequestHandler):
    lock = threading.Lock()
    runs = {}
    posts = []

    def log_message(self, *_):
        pass

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith('/users'):
            return self.reply({'user_id': body['user_name']}, 201)
        if self.headers.get('X-DTest-Mock-LLM') != '1':
            return self.reply({'error': 'Missing mock header'}, 400)
        with self.lock:
            self.posts.append(body)
            step = self.runs[body['run_id']]['step'] + 1 if 'run_id' in body else 0
            rid = str(len(self.runs) + 1)
            self.runs[rid] = {'step': step, 'task': body.get('task_id', rid)}
        self.reply({'task_id': self.runs[rid]['task'], 'submission_run_id': rid}, 202)

    def do_GET(self):
        if self.path.endswith('/projects'):
            return self.reply({'items': [{'id': 'project', 'is_default': True}]})
        rid = self.path.rsplit('/', 1)[-1]
        state = self.runs[rid]
        step = state['step']
        run = dict(id=rid, task_id=state['task'], run_status='interrupted', queue_wait_seconds=0.125,
            node_timings={f'node{step}': dict(wall_seconds=0.25, count=1, min_seconds=0.25, max_seconds=0.25)})
        if step == 3:
            run.update(run_status='running', interrupt=[{'kind': 'EXECUTOR_EVENT'}])
        else:
            names = ['workflow_candidate_selection', 'additional_information', 'workflow_approval']
            form_tools = {}
            for field in load.SCENARIO['fields']:
                key = (field['step_index'], field['tool_index'])
                tool = form_tools.setdefault(key, dict(step_index=key[0], tool_index=key[1], parameters=[]))
                parameter = dict(name=field['parameter'], editable=True)
                if field['argument']['source'] == 'tool_output':
                    parameter['output_options'] = [field['argument']]
                tool['parameters'].append(parameter)
            args = ({'candidates': [{'candidate_id': 'candidate'}]} if step == 0 else
                    {'workflow_id': 'workflow', 'workflow_revision': 1,
                     'tool_inputs': list(form_tools.values())} if step == 1 else {})
            run['interactions'] = [dict(interaction_id='i'+rid, revision=1, payload_version=2,
                                       action_requests=[dict(name=names[step], args=args)])]
        self.reply(run)


class LoadTests(unittest.TestCase):
    def test_missing_metrics_are_errors(self):
        for run in ({}, {'queue_wait_seconds': 0, 'node_timings': {}},
                    {'queue_wait_seconds': float('nan')}):
            with self.assertRaises(ValueError):
                load.validate_metrics(run)

    def test_complete_concurrent_http_flow_and_reports(self):
        Handler.runs, Handler.posts = {}, []
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                out = Path(folder) / 'results'
                code = load.main(['--host', f'http://127.0.0.1:{server.server_port}',
                    '--users', '1', '5', '--rounds', '2', '--out-dir', str(out)])
                self.assertEqual(code, 0)
                samples = [json.loads(line) for line in (out / 'samples.jsonl').read_text().splitlines()]
                self.assertEqual(len(samples), 12)
                self.assertTrue(all(len(s['runs']) == 4 for s in samples))
                self.assertTrue(all(not s['error'] for s in samples))
                self.assertEqual(len((out / 'queue_wait.csv').read_text(encoding='utf-8-sig').splitlines()), 49)
                self.assertEqual(len((out / 'node_timings.csv').read_text(encoding='utf-8-sig').splitlines()), 49)
                edits = [b['command']['resume'] for b in Handler.posts if 'command' in b]
                self.assertEqual(sum(e['action'] == 'approve' for e in edits), 12)
                inputs = [e['payload']['inputs'] for e in edits if e.get('payload', {}).get('intent') == 'submit_inputs']
                self.assertTrue(all(next(p['value'] for p in i if p['parameter'] == 'lot_cd') == 'LOT1' for i in inputs))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
