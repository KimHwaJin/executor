"""Locust graph timing test from first message to Executor submission.

Run the API server first, then run this Locust file against it. Each Locust user
creates one disposable account and executes GRAPH_REPEATS_PER_USER independent
conversation sessions.

Example:
    GRAPH_REPEATS_PER_USER=3 BOTTLENECK_REPORT=var/load/report.json \
      locust -f src/tests/load/locust_graph_mock_llm.py \
      --host http://127.0.0.1:8012 --headless -u 20 -r 5 -t 900s
"""

from __future__ import annotations

import json
import os
import argparse
import time
from pathlib import Path
from uuid import uuid4

import gevent
from locust import HttpUser, between, events, task
from locust.env import Environment
from locust.exception import StopUser


CHAT_PATH = os.environ.get("CHAT_COMPLETION_PATH", "/api/v1/chat/completion")
API_PREFIX = os.environ.get("API_PREFIX", "/api/v1").rstrip("/")
MESSAGE_FIELD = os.environ.get("CHAT_MESSAGE_FIELD", "content_text")
MOCK_LLM_SLEEP_SECONDS = os.environ.get("MOCK_LLM_SLEEP_SECONDS", "0")
PROMPT = os.environ.get(
    "COMPLETION_PROMPT",
    "Select mock data and build an analysis workflow. Proceed until the executor request is submitted.",
)
COMPLETION_TIMEOUT = float(os.environ.get("COMPLETION_TIMEOUT", "900"))
REPEATS_PER_USER = int(os.environ.get("GRAPH_REPEATS_PER_USER", "1"))
WAIT_MIN_SECONDS = float(os.environ.get("LOCUST_WAIT_MIN_SECONDS", "0.3"))
WAIT_MAX_SECONDS = float(os.environ.get("LOCUST_WAIT_MAX_SECONDS", "0.7"))
MAX_HITL_STEPS = int(os.environ.get("MAX_HITL_STEPS", "20"))

APPROVE = {"decisions": [{"type": "approve"}]}
DEFAULT_COMMANDS = {
    "data_selection": "mock",
    "analysis_context": APPROVE,
    "workflow_candidate_selection": APPROVE,
    "workflow_approval": APPROVE,
}

_samples: list[dict] = []
_case_errors: list[str] = []
_users_spawned = False
_quit_scheduled = False
_sweep_owns_report = False

MOCK_TOOL_VALUES = {
    "data_type": "nce",
    "lot_cd": "LOT1",
    "process": ["P01"],
    "start_dt": "2026-01-01",
    "end_dt": "2026-02-01",
    "query_mode": "period",
}


class GraphTimingUser(HttpUser):
    wait_time = between(WAIT_MIN_SECONDS, WAIT_MAX_SECONDS)

    def call(self, body: dict, *, key: str | None = None) -> dict:
        headers = dict(getattr(self, "auth", {}))
        if key:
            headers["Idempotency-Key"] = key

        kind = body["type"]
        method, path, data = "POST", CHAT_PATH, body
        if kind == "signup":
            path, data = f"{API_PREFIX}/users", body["user"]
        elif kind == "projects":
            method, path, data = "GET", f"{API_PREFIX}/projects", None
        elif kind == "session_create":
            path, data = f"{API_PREFIX}/projects/{body['project_id']}/sessions", {}

        with self.client.request(
            method,
            path,
            json=data,
            headers=headers,
            name=f"graph/{kind}",
            catch_response=True,
            timeout=60,
        ) as response:
            if response.status_code not in (200, 201, 202):
                error = getattr(response, "error", None)
                detail = str(error) if error is not None else response.text[:500]
                message = f"{method} {path} graph/{kind}: HTTP {response.status_code}: {detail}"
                _case_errors.append(message)
                response.failure(message)
                raise StopUser()
            try:
                return response.json()
            except ValueError:
                message = f"{method} {path} graph/{kind}: Expected JSON response"
                _case_errors.append(message)
                response.failure(message)
                raise StopUser()

    def on_start(self) -> None:
        self.completed_cycles = 0
        self._reset_cycle_state()
        user = self.call(
            {
                "type": "signup",
                "user": {"user_name": "locust-graph-" + uuid4().hex},
            }
        )
        user_id = str(user["user_id"])
        self.auth = {
            "Authorization": f"Bearer {user_id}",
            "X-User-ID": user_id,
            "X-DTest-Mock-LLM": "1",
            "X-DTest-Mock-LLM-Sleep": MOCK_LLM_SLEEP_SECONDS,
        }
        projects = self.call({"type": "projects"})["items"]
        self.project_id = next(
            (project["id"] for project in projects if project.get("is_default")),
            projects[0]["id"],
        )

    def _reset_cycle_state(self) -> None:
        self.run = None
        self.session_id = None
        self.started = None
        self.steps = 0
        self.slept = 0.0
        self.queue_wait = 0.0
        self.nodes: dict[str, dict[str, float]] = {}
        self.noted_runs: set[str] = set()
        self.observations: list[dict] = []

    def note_run(self) -> None:
        timings = self.run.get("node_timings") or {}
        self.observations.append(
            {
                "run_id": self.run.get("id"),
                "status": self.run.get("status"),
                "kind": _interrupt_kind(self.run),
                "has_node_timings": bool(timings),
                "node_timing_count": len(timings) if isinstance(timings, dict) else 0,
                "keys": sorted(self.run.keys()),
            }
        )
        if self.run.get("status") == "pending":
            return
        if not timings:
            return
        run_id = str(self.run.get("id") or "")
        if not run_id or run_id in self.noted_runs:
            return
        self.noted_runs.add(run_id)
        self.slept += float(self.run.get("mock_llm_sleep_seconds") or 0)
        self.queue_wait += float(self.run.get("queue_wait_seconds") or 0)
        for name, bucket in timings.items():
            if not isinstance(bucket, dict):
                continue
            total = self.nodes.setdefault(
                name,
                {
                    "wall_seconds": 0.0,
                    "count": 0,
                    "min_seconds": None,
                    "max_seconds": 0.0,
                },
            )
            total["wall_seconds"] += float(bucket.get("wall_seconds") or 0)
            total["count"] += int(bucket.get("count") or 0)
            minimum = bucket.get("min_seconds")
            maximum = bucket.get("max_seconds")
            if minimum is not None:
                total["min_seconds"] = (
                    float(minimum)
                    if total["min_seconds"] is None
                    else min(float(total["min_seconds"]), float(minimum))
                )
            if maximum is not None:
                total["max_seconds"] = max(float(total["max_seconds"]), float(maximum))

    def record_cycle(self, error: str | None = None) -> None:
        elapsed = time.monotonic() - self.started
        _samples.append(
            {
                "elapsed": elapsed,
                "slept": self.slept,
                "queue_wait": self.queue_wait,
                "nodes": self.nodes,
                "observations": self.observations[-20:],
                "error": error,
            }
        )
        self.environment.events.request.fire(
            request_type="FLOW",
            name="graph_to_executor_submission",
            response_time=elapsed * 1000,
            response_length=0,
            exception=RuntimeError(error) if error else None,
        )
        self.environment.events.request.fire(
            request_type="FLOW",
            name="graph_to_executor_excluding_mock_sleep",
            response_time=max(0.0, elapsed - self.slept) * 1000,
            response_length=0,
            exception=RuntimeError(error) if error else None,
        )

    def finish_cycle(self, error: str | None = None) -> None:
        if error is None and not self.nodes:
            self.refresh_timings_before_finish()
        self.record_cycle(error)
        self.completed_cycles += 1
        self._reset_cycle_state()
        if error or self.completed_cycles >= REPEATS_PER_USER:
            raise StopUser()

    def refresh_timings_before_finish(self) -> None:
        if not self.session_id or not self.run or not self.run.get("id"):
            return
        for _ in range(3):
            self.run = self.call(
                {
                    "type": "status",
                    "session_id": self.session_id,
                    "run_id": self.run["id"],
                }
            )
            self.note_run()
            if self.nodes:
                return
            gevent.sleep(0.5)

    @task
    def conversation(self) -> None:
        if self.run is None:
            self.started = time.monotonic()
            self.steps = 0
            self.session_id = self.call(
                {"type": "session_create", "project_id": self.project_id}
            )["id"]
            message_body = {
                "type": "message",
                "session_id": self.session_id,
                MESSAGE_FIELD: PROMPT,
            }
            if MESSAGE_FIELD == "message":
                message_body["user_id"] = self.auth["X-User-ID"]
            self.run = self.call(
                message_body,
                key=uuid4().hex,
            )
            return

        if time.monotonic() - self.started > COMPLETION_TIMEOUT:
            self.finish_cycle("Conversation timed out")

        self.run = self.call(
            {
                "type": "status",
                "session_id": self.session_id,
                "run_id": self.run["id"],
            }
        )
        self.note_run()

        status = self.run["status"]
        kind = _interrupt_kind(self.run)
        if status == "running" and kind == "EXECUTOR_EVENT":
            self.finish_cycle()
            return
        if status in ("pending", "running"):
            return
        if status in ("error", "timeout", "canceled"):
            self.finish_cycle(
                f"Unexpected terminal status: {status}: {self.run.get('failure')}"
            )
            return
        if status == "success":
            self.finish_cycle()
            return
        if status != "interrupted":
            self.finish_cycle(f"Unknown status: {status}")
            return

        self.steps += 1
        if self.steps > MAX_HITL_STEPS:
            self.finish_cycle("Too many HITL steps")
            return

        try:
            command = _resume_command(self.run)
        except RuntimeError as exc:
            self.finish_cycle(str(exc))
            return
        self.run = self.call(
            {
                "type": "resume",
                "session_id": self.session_id,
                "run_id": self.run["id"],
                "command": command,
            },
            key=uuid4().hex,
        )

    def on_stop(self) -> None:
        if getattr(self, "started", None) is not None:
            self.record_cycle("Stopped before graph cycle completed")
            self._reset_cycle_state()
        _quit_after_last_user(self.environment)


def _interrupt_item(run: dict) -> dict:
    item = (run.get("interrupt") or [{}])[0]
    return item if isinstance(item, dict) else {}


def _interrupt_action(run: dict) -> dict:
    action = (_interrupt_item(run).get("action_requests") or [{}])[0]
    return action if isinstance(action, dict) else {}


def _interrupt_kind(run: dict) -> str:
    item = _interrupt_item(run)
    action = _interrupt_action(run)
    return str(action.get("name") or item.get("kind") or "")


def _edit_decision(action_name: str, args: dict) -> dict:
    return {
        "decisions": [
            {
                "type": "edit",
                "edited_action": {"name": action_name, "args": args},
            }
        ]
    }


def _resume_command(run: dict):
    kind = _interrupt_kind(run)
    args = _interrupt_action(run).get("args") or {}
    if kind == "workflow_candidate_selection" and isinstance(args, dict) and "candidates" in args:
        candidates = args.get("candidates") or []
        if not candidates:
            raise RuntimeError("Candidate selection has no candidates")
        candidate_id = candidates[0].get("candidate_id") or candidates[0].get("id")
        if not candidate_id:
            raise RuntimeError("Candidate has no candidate_id")
        return _edit_decision(
            "workflow_candidate_selection",
            {"intent": "select", "candidate_id": candidate_id},
        )
    if kind == "additional_information" and isinstance(args, dict) and "tool_inputs" in args:
        return _edit_decision("additional_information", _tool_input_submission(args))
    if kind == "additional_information":
        return _additional_information_command(run)
    if kind == "workflow_approval":
        return APPROVE
    if kind not in DEFAULT_COMMANDS:
        raise RuntimeError(f"Unsupported HITL interrupt: {kind}")
    return DEFAULT_COMMANDS[kind]


def _schema_branch(schema: dict) -> dict:
    for branch in schema.get("anyOf") or []:
        if isinstance(branch, dict) and branch.get("type") != "null":
            return branch
    return schema


def _mock_parameter_value(parameter: dict):
    name = str(parameter.get("name") or "")
    if parameter.get("has_value"):
        return parameter.get("value")
    if name in MOCK_TOOL_VALUES:
        return MOCK_TOOL_VALUES[name]
    if parameter.get("has_default"):
        return parameter.get("default_value")
    schema = _schema_branch(parameter.get("value_schema") or {})
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "string":
        return "mock"
    if kind == "integer":
        return 1
    if kind == "number":
        return 1
    if kind == "boolean":
        return True
    if kind == "array":
        return ["P01"] if name == "process" else ["mock"]
    raise RuntimeError(f"Provide a mock value for {name}")


def _tool_input_submission(args: dict) -> dict:
    inputs = []
    for tool in args.get("tool_inputs") or []:
        for parameter in tool.get("parameters") or []:
            if not parameter.get("editable"):
                continue
            binding = parameter.get("binding")
            if binding:
                reference = {k: binding[k] for k in ("step_index", "tool_index", "output")}
                offered = [
                    {k: option[k] for k in ("step_index", "tool_index", "output")}
                    for option in parameter.get("output_options") or []
                ]
                # Omitting a bound parameter keeps the connection.
                if reference in offered:
                    inputs.append(
                        {
                            "step_index": tool["step_index"],
                            "tool_index": tool["tool_index"],
                            "parameter": parameter["name"],
                            "source": "tool_output",
                            "reference": reference,
                        }
                    )
                continue
            if not parameter.get("required") and not parameter.get("has_value"):
                continue
            inputs.append(
                {
                    "step_index": tool["step_index"],
                    "tool_index": tool["tool_index"],
                    "parameter": parameter["name"],
                    "value": _mock_parameter_value(parameter),
                }
            )
    return {
        "intent": "submit_inputs",
        "workflow_id": args["workflow_id"],
        "workflow_revision": args["workflow_revision"],
        "inputs": inputs,
    }


def _additional_information_command(run: dict) -> dict:
    item = (run.get("interrupt") or [{}])[0]
    action = (item.get("action_requests") or [{}])[0]
    config = (item.get("review_configs") or [{}])[0]
    properties = (config.get("args_schema") or item.get("schema") or {}).get(
        "properties",
        {},
    )
    defaults = {
        "join_columns": ["alias_lot_id", "wf_id", "x", "y"],
        "target_column": "fail",
        "test_size": 0.2,
        "sample_size": 1000,
    }
    defaults.update(json.loads(os.environ.get("COMPLETION_ANSWERS", "{}")))
    answers = {}
    for name, schema in properties.items():
        value = defaults.get(name, (action.get("args") or {}).get(name))
        if value is None or value == "":
            value = schema.get("default")
        if value is None:
            raise RuntimeError(f"Provide COMPLETION_ANSWERS for {name}")
        answers[name] = value
    return {"answers": answers}


def _quit_after_last_user(environment) -> None:
    def _check(_greenlet):
        global _quit_scheduled
        runner = environment.runner
        if (
            not _users_spawned
            or _quit_scheduled
            or runner is None
            or runner.user_count != 0
        ):
            return
        _quit_scheduled = True
        gevent.spawn(runner.quit)

    gevent.getcurrent().link(_check)


@events.test_start.add_listener
def _reset_samples(**_kwargs) -> None:
    global _users_spawned, _quit_scheduled
    _samples.clear()
    _case_errors.clear()
    _users_spawned = False
    _quit_scheduled = False


@events.spawning_complete.add_listener
def _mark_users_spawned(**_kwargs) -> None:
    global _users_spawned
    _users_spawned = True


def _span(values: list[float]) -> dict[str, float]:
    if not values:
        return {"avg": 0.0, "min": 0.0, "max": 0.0}
    return {
        "avg": round(sum(values) / len(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _summary(users) -> dict:
    ok = [sample for sample in _samples if not sample["error"]]
    cycles = [sample["elapsed"] for sample in ok]
    cycles_without_sleep = [max(0.0, sample["elapsed"] - sample["slept"]) for sample in ok]
    queues = [sample["queue_wait"] for sample in ok]
    totals: dict[str, dict[str, float]] = {}
    for sample in ok:
        for name, bucket in sample["nodes"].items():
            total = totals.setdefault(
                name,
                {
                    "wall_seconds": 0.0,
                    "count": 0,
                    "min_seconds": None,
                    "max_seconds": 0.0,
                },
            )
            total["wall_seconds"] += bucket["wall_seconds"]
            total["count"] += int(bucket["count"])
            if bucket.get("min_seconds") is not None:
                total["min_seconds"] = (
                    float(bucket["min_seconds"])
                    if total["min_seconds"] is None
                    else min(float(total["min_seconds"]), float(bucket["min_seconds"]))
                )
            total["max_seconds"] = max(
                float(total["max_seconds"]),
                float(bucket.get("max_seconds") or 0),
            )

    nodes = []
    for name, bucket in sorted(
        totals.items(),
        key=lambda item: item[1]["wall_seconds"],
        reverse=True,
    ):
        count = int(bucket["count"]) or 1
        nodes.append(
            {
                "name": name,
                "sum_seconds": round(bucket["wall_seconds"], 3),
                "avg_seconds": round(bucket["wall_seconds"] / count, 3),
                "min_seconds": round(float(bucket["min_seconds"] or 0), 3),
                "max_seconds": round(float(bucket["max_seconds"] or 0), 3),
                "count": int(bucket["count"]),
            }
        )

    return {
        "users": users,
        "repeats_per_user": REPEATS_PER_USER,
        "cycles": len(_samples),
        "success": len(ok),
        "failed": len(_samples) - len(ok),
        "cycle_seconds": _span(cycles),
        "cycle_excluding_mock_sleep_seconds": _span(cycles_without_sleep),
        "queue_wait_seconds": _span(queues),
        "nodes": nodes,
        "observations": [
            observation
            for sample in _samples
            for observation in sample.get("observations", [])
        ][-50:],
        "errors": (
            _case_errors
            + [sample["error"] for sample in _samples if sample["error"]]
        )[:10],
    }


def _write_summary(report: dict) -> None:
    path = os.environ.get("BOTTLENECK_REPORT", "").strip()
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = os.environ.get("BOTTLENECK_SUMMARY", "").strip()
    slowest = report["nodes"][0] if report["nodes"] else {}
    if summary:
        summary_path = Path(summary)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        if not summary_path.exists():
            summary_path.write_text(
                "users,repeats_per_user,total_requested,cycles,success,failed,"
                "cycle_avg_s,cycle_max_s,cycle_no_sleep_avg_s,queue_avg_s,"
                "slowest_node,slowest_avg_s,slowest_count\n",
                encoding="utf-8",
            )
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write(
                ",".join(
                    [
                        str(report["users"]),
                        str(report["repeats_per_user"]),
                        str(report["users"] * report["repeats_per_user"]),
                        str(report["cycles"]),
                        str(report["success"]),
                        str(report["failed"]),
                        str(report["cycle_seconds"]["avg"]),
                        str(report["cycle_seconds"]["max"]),
                        str(report["cycle_excluding_mock_sleep_seconds"]["avg"]),
                        str(report["queue_wait_seconds"]["avg"]),
                        str(slowest.get("name", "")),
                        str(slowest.get("avg_seconds", "")),
                        str(slowest.get("count", "")),
                    ]
                )
                + "\n"
            )

    node_summary = os.environ.get("BOTTLENECK_NODE_SUMMARY", "").strip()
    if not node_summary:
        return
    node_summary_path = Path(node_summary)
    node_summary_path.parent.mkdir(parents=True, exist_ok=True)
    if not node_summary_path.exists():
        node_summary_path.write_text(
            "users,repeats_per_user,total_requested,node,"
            "sum_seconds,avg_seconds,min_seconds,max_seconds,count\n",
            encoding="utf-8",
        )
    with node_summary_path.open("a", encoding="utf-8") as handle:
        for node in report["nodes"]:
            node_name = str(node["name"]).replace(",", "_")
            handle.write(
                ",".join(
                    [
                        str(report["users"]),
                        str(report["repeats_per_user"]),
                        str(report["users"] * report["repeats_per_user"]),
                        node_name,
                        str(node["sum_seconds"]),
                        str(node["avg_seconds"]),
                        str(node["min_seconds"]),
                        str(node["max_seconds"]),
                        str(node["count"]),
                    ]
                )
                + "\n"
            )


@events.quitting.add_listener
def _print_bottleneck(environment, **_kwargs) -> None:
    if _sweep_owns_report:
        return
    users = getattr(getattr(environment, "runner", None), "target_user_count", None)
    report = _summary(users)
    _write_summary(report)
    if not _samples:
        return
    print(
        "\nnode timing summary "
        f"users={report['users']} repeats={report['repeats_per_user']} "
        f"cycles={report['cycles']} success={report['success']} failed={report['failed']}"
    )
    cycle = report["cycle_seconds"]
    no_sleep = report["cycle_excluding_mock_sleep_seconds"]
    queue = report["queue_wait_seconds"]
    print(
        f"cycle avg={cycle['avg']:.3f}s max={cycle['max']:.3f}s | "
        f"excluding_mock_sleep avg={no_sleep['avg']:.3f}s | "
        f"queue_wait avg={queue['avg']:.3f}s max={queue['max']:.3f}s"
    )
    print("slowest nodes")
    for bucket in report["nodes"][:12]:
        print(
            f"  {bucket['name']:40} "
            f"sum={bucket['sum_seconds']:8.3f}s "
            f"avg={bucket['avg_seconds']:8.3f}s "
            f"min={bucket['min_seconds']:8.3f}s "
            f"max={bucket['max_seconds']:8.3f}s "
            f"n={bucket['count']}"
        )


def _split_levels(value: str) -> list[int]:
    levels = [int(item) for item in value.replace(",", " ").split() if item.strip()]
    if not levels or any(level <= 0 for level in levels):
        raise argparse.ArgumentTypeError("levels must be positive integers")
    return levels


def _run_sweep() -> int:
    parser = argparse.ArgumentParser(
        description="Run Locust graph timing sweep and write results under var/yyyyMMddHHmmss."
    )
    parser.add_argument("--host", default=os.environ.get("LOCUST_HOST") or os.environ.get("SERVER_URL") or "http://127.0.0.1:8000")
    parser.add_argument("--api-prefix", default=os.environ.get("API_PREFIX", "/api/v1"))
    parser.add_argument("--chat-path", default=os.environ.get("CHAT_COMPLETION_PATH", "/api/v1/chat/completion"))
    parser.add_argument("--message-field", choices=["message", "content_text"], default=os.environ.get("CHAT_MESSAGE_FIELD", "content_text"))
    parser.add_argument("--users", type=_split_levels, default=_split_levels(os.environ.get("SWEEP_USERS", "1 5 10 20 50 100 150")))
    parser.add_argument("--repeats", type=_split_levels, default=_split_levels(os.environ.get("SWEEP_REPEATS", "1 2 3 4 5")))
    parser.add_argument("--spawn-rate", type=int, default=int(os.environ.get("SWEEP_SPAWN_RATE", "5")))
    parser.add_argument("--mock-llm-sleep", default=os.environ.get("MOCK_LLM_SLEEP_SECONDS", "0"))
    parser.add_argument("--out-dir", default=os.environ.get("BOTTLENECK_OUT"))
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d%H%M%S")
    out_dir = Path(args.out_dir or Path("var") / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "summary.csv"
    node_summary_path = out_dir / "nodes.csv"

    exit_code = 0
    for repeats in args.repeats:
        for users in args.users:
            limit = 180 + users * repeats * 90
            tag = f"users-{users:03d}-repeat-{repeats}"
            print(f"=== locust users={users} repeats_per_user={repeats} timeout={limit}s ===", flush=True)
            _configure_case(
                api_prefix=args.api_prefix,
                chat_path=args.chat_path,
                message_field=args.message_field,
                mock_llm_sleep=args.mock_llm_sleep,
                repeats=repeats,
                timeout_seconds=limit,
                report_path=out_dir / f"{tag}.json",
                summary_path=summary_path,
                node_summary_path=node_summary_path,
            )
            failed = _run_one_case(
                host=args.host,
                users=users,
                spawn_rate=args.spawn_rate,
                max_seconds=limit + 60,
                log_path=out_dir / f"{tag}.log",
            )
            if failed:
                exit_code = 1

    excel_path = _write_excel(out_dir, summary_path, node_summary_path)
    print(f"report directory: {out_dir}", flush=True)
    print(f"summary csv: {summary_path}", flush=True)
    print(f"node timing csv: {node_summary_path}", flush=True)
    print(f"excel report: {excel_path}", flush=True)
    return exit_code


def _write_excel(out_dir: Path, summary_path: Path, node_summary_path: Path) -> Path:
    import pandas as pd

    excel_path = out_dir / "bottleneck.xlsx"
    summary = pd.read_csv(summary_path) if summary_path.exists() else pd.DataFrame()
    nodes = pd.read_csv(node_summary_path) if node_summary_path.exists() else pd.DataFrame()
    errors = []
    for report_path in sorted(out_dir.glob("users-*.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for message in report.get("errors") or []:
            errors.append(
                {
                    "users": report.get("users"),
                    "repeats_per_user": report.get("repeats_per_user"),
                    "error": message,
                }
            )
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        nodes.to_excel(writer, sheet_name="nodes", index=False)
        pd.DataFrame(errors).to_excel(writer, sheet_name="errors", index=False)
        if not nodes.empty:
            for repeats, frame in nodes.groupby("repeats_per_user"):
                pivot = frame.pivot_table(
                    index="node",
                    columns="users",
                    values="avg_seconds",
                    aggfunc="mean",
                )
                pivot.to_excel(writer, sheet_name=f"avg_repeat_{repeats}"[:31])
    return excel_path


def _configure_case(
    *,
    api_prefix: str,
    chat_path: str,
    message_field: str,
    mock_llm_sleep: str,
    repeats: int,
    timeout_seconds: float,
    report_path: Path,
    summary_path: Path,
    node_summary_path: Path,
) -> None:
    global API_PREFIX, CHAT_PATH, MESSAGE_FIELD, MOCK_LLM_SLEEP_SECONDS, REPEATS_PER_USER, COMPLETION_TIMEOUT

    API_PREFIX = api_prefix.rstrip("/")
    CHAT_PATH = chat_path
    MESSAGE_FIELD = message_field
    MOCK_LLM_SLEEP_SECONDS = str(mock_llm_sleep)
    REPEATS_PER_USER = repeats
    COMPLETION_TIMEOUT = float(timeout_seconds)
    os.environ["BOTTLENECK_REPORT"] = str(report_path)
    os.environ["BOTTLENECK_SUMMARY"] = str(summary_path)
    os.environ["BOTTLENECK_NODE_SUMMARY"] = str(node_summary_path)


def _run_one_case(
    *,
    host: str,
    users: int,
    spawn_rate: int,
    max_seconds: float,
    log_path: Path,
) -> bool:
    global _users_spawned, _quit_scheduled, _sweep_owns_report

    _sweep_owns_report = True
    _samples.clear()
    _case_errors.clear()
    _users_spawned = False
    _quit_scheduled = False

    environment = Environment(user_classes=[GraphTimingUser], host=host)
    runner = environment.create_local_runner()
    started = time.monotonic()
    timed_out = False

    with log_path.open("w", encoding="utf-8") as log:
        def write(message: str) -> None:
            print(message, flush=True)
            log.write(message + "\n")
            log.flush()

        expected_cycles = users * REPEATS_PER_USER
        runner.start(user_count=users, spawn_rate=spawn_rate)
        _users_spawned = True
        while len(_samples) < expected_cycles:
            if time.monotonic() - started > max_seconds:
                timed_out = True
                write(f"case timed out after {max_seconds:.0f}s; stopping runner")
                break
            if runner.user_count == 0 and _case_errors:
                break
            if runner.user_count == 0 and len(_samples) > 0:
                break
            gevent.sleep(1.0)
        runner.quit()
        runner.greenlet.join(timeout=10)
        report = _summary(users)
        if timed_out:
            report["errors"].append(f"case timed out after {max_seconds:.0f}s")
            report["failed"] += max(0, users * REPEATS_PER_USER - report["cycles"])
        elif _case_errors and report["cycles"] < expected_cycles:
            report["failed"] += expected_cycles - report["cycles"]
        _write_summary(report)
        slowest = report["nodes"][0] if report["nodes"] else {}
        write(
            "result "
            f"cycles={report['cycles']} success={report['success']} failed={report['failed']} "
            f"slowest={slowest.get('name', '')} avg={slowest.get('avg_seconds', '')}"
        )
        for error in report["errors"]:
            write(f"error {error}")
    return bool(report["failed"])


if __name__ == "__main__":
    raise SystemExit(_run_sweep())
