"""Locust graph timing test for the workflow_run API, from first input to Executor submission.

Each Locust user creates one disposable account and runs REPEATS_PER_USER tasks through
the v2 HITL steps: candidate selection -> tool inputs -> plan approval -> Executor.
Defaults are the constants below; override any of them with command-line options (--help).
Results are written to var/yyyyMMddHHmmss (summary.csv, nodes.csv, bottleneck.xlsx).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import gevent
from locust import HttpUser, between, events, task
from locust.env import Environment
from locust.exception import StopUser


HOST = "http://127.0.0.1:8012"
API_PREFIX = "/api/v1"
USERS = [1, 5, 10, 20, 50, 100, 150]
REPEATS = [1, 2, 3, 4, 5]
SPAWN_RATE = 5
MOCK_LLM_SLEEP_SECONDS = "0"
PROMPT = "NCE 데이터 품질을 확인해줘"
MAX_HITL_STEPS = 20
WAIT_MIN_SECONDS = 0.3
WAIT_MAX_SECONDS = 0.7
OUT_DIR = None

# Values for required extract_data inputs that the mock workflow leaves to the user.
MOCK_TOOL_VALUES = {
    "data_type": "nce",
    "lot_cd": "LOT1",
    "process": ["P01"],
    "start_dt": "2026-01-01",
    "end_dt": "2026-02-01",
    "query_mode": "period",
}

WORKFLOW_RUN_PATH = f"{API_PREFIX}/workflow_run"
REPEATS_PER_USER = 1
COMPLETION_TIMEOUT = 900.0
REPORT_PATH: Path | None = None
SUMMARY_PATH: Path | None = None
NODE_SUMMARY_PATH: Path | None = None

_samples: list[dict] = []
_case_errors: list[str] = []
_users_spawned = False
_quit_scheduled = False
_sweep_owns_report = False


class WorkflowRunTimingUser(HttpUser):
    wait_time = between(WAIT_MIN_SECONDS, WAIT_MAX_SECONDS)

    def call(
        self,
        name: str,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        key: str | None = None,
    ) -> dict:
        headers = dict(getattr(self, "auth", {}))
        if key:
            headers["Idempotency-Key"] = key
        with self.client.request(
            method,
            path,
            json=body,
            headers=headers,
            name=f"workflow_run/{name}",
            catch_response=True,
            timeout=60,
        ) as response:
            if response.status_code not in (200, 201, 202):
                error = getattr(response, "error", None)
                detail = str(error) if error is not None else response.text[:500]
                message = f"{method} {path} workflow_run/{name}: HTTP {response.status_code}: {detail}"
                _case_errors.append(message)
                response.failure(message)
                raise StopUser()
            try:
                return response.json()
            except ValueError:
                message = f"{method} {path} workflow_run/{name}: Expected JSON response"
                _case_errors.append(message)
                response.failure(message)
                raise StopUser()

    def on_start(self) -> None:
        self.completed_cycles = 0
        self._reset_cycle_state()
        user = self.call(
            "signup",
            "POST",
            f"{API_PREFIX}/users",
            {"user_name": "locust-workflow-run-" + uuid4().hex},
        )
        user_id = str(user["user_id"])
        self.auth = {
            "X-User-ID": user_id,
            "X-DTest-Mock-LLM": "1",
            "X-DTest-Mock-LLM-Sleep": MOCK_LLM_SLEEP_SECONDS,
        }
        projects = self.call("projects", "GET", f"{API_PREFIX}/projects")["items"]
        self.project_id = next(
            (project["id"] for project in projects if project.get("is_default")),
            projects[0]["id"],
        )

    def _reset_cycle_state(self) -> None:
        self.run = None
        self.task_id = None
        self.started = None
        self.steps = 0
        self.slept = 0.0
        self.queue_wait = 0.0
        self.nodes: dict[str, dict[str, float]] = {}
        self.noted_runs: set[str] = set()
        self.observations: list[dict] = []

    def poll(self) -> None:
        self.run = self.call(
            "status",
            "GET",
            f"{WORKFLOW_RUN_PATH}/{self.run['id']}",
        )
        self.note_run()

    def submit(self, name: str, body: dict) -> None:
        snapshot = self.call(name, "POST", WORKFLOW_RUN_PATH, body, key=uuid4().hex)
        self.task_id = snapshot["task_id"]
        self.run = {"id": snapshot["submission_run_id"], "run_status": "pending"}

    def note_run(self) -> None:
        timings = self.run.get("node_timings") or {}
        self.observations.append(
            {
                "run_id": self.run.get("id"),
                "task_id": self.run.get("task_id"),
                "status": self.run.get("run_status"),
                "kind": _run_kind(self.run),
                "has_node_timings": bool(timings),
                "node_timing_count": len(timings) if isinstance(timings, dict) else 0,
                "keys": sorted(self.run.keys()),
            }
        )
        if self.run.get("run_status") == "pending":
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
        if not self.run or not self.run.get("id"):
            return
        for _ in range(3):
            self.poll()
            if self.nodes:
                return
            gevent.sleep(0.5)

    @task
    def conversation(self) -> None:
        if self.run is None:
            self.started = time.monotonic()
            self.steps = 0
            self.submit(
                "new",
                {
                    "input": {"content": [{"type": "text", "text": PROMPT}]},
                    "project_id": self.project_id,
                },
            )
            return

        if time.monotonic() - self.started > COMPLETION_TIMEOUT:
            self.finish_cycle("Conversation timed out")

        self.poll()

        status = self.run["run_status"]
        if status == "running" and _executor_waiting(self.run):
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
            interaction = _open_interaction(self.run)
            action, payload = _resume_decision(interaction)
        except RuntimeError as exc:
            self.finish_cycle(str(exc))
            return
        resume = {
            "action": action,
            "interaction_id": interaction["interaction_id"],
            "revision": interaction["revision"],
        }
        if payload is not None:
            resume["payload"] = payload
        self.submit(
            f"resume/{_interaction_name(interaction)}",
            {
                "task_id": self.task_id,
                "run_id": self.run["id"],
                "command": {"resume": resume},
            },
        )

    def on_stop(self) -> None:
        if getattr(self, "started", None) is not None:
            self.record_cycle("Stopped before graph cycle completed")
            self._reset_cycle_state()
        _quit_after_last_user(self.environment)


def _executor_waiting(run: dict) -> bool:
    return any(
        isinstance(item, dict) and item.get("kind") == "EXECUTOR_EVENT"
        for item in run.get("interrupt") or []
    )


def _interaction_name(interaction: dict) -> str:
    action = (interaction.get("action_requests") or [{}])[0]
    return str(action.get("name") or interaction.get("kind") or "")


def _run_kind(run: dict) -> str:
    if _executor_waiting(run):
        return "EXECUTOR_EVENT"
    interactions = run.get("interactions") or []
    return _interaction_name(interactions[0]) if interactions else ""


def _open_interaction(run: dict) -> dict:
    interactions = [
        item
        for item in run.get("interactions") or []
        if _interaction_name(item) != "next_user_request"
    ]
    if len(interactions) != 1:
        raise RuntimeError(f"Expected one open interaction, got {len(interactions)}")
    return interactions[0]


def _resume_decision(interaction: dict) -> tuple[str, dict | None]:
    name = _interaction_name(interaction)
    if interaction.get("payload_version", 1) != 2:
        raise RuntimeError(f"Unsupported HITL payload_version for {name}")
    args = (interaction.get("action_requests") or [{}])[0].get("args") or {}
    if name == "workflow_candidate_selection":
        candidates = args.get("candidates") or []
        if not candidates:
            raise RuntimeError("Candidate selection has no candidates")
        return "edit", {"intent": "select", "candidate_id": candidates[0]["candidate_id"]}
    if name == "additional_information":
        return "edit", _tool_input_submission(args)
    if name == "workflow_approval":
        return "approve", None
    raise RuntimeError(f"Unsupported HITL interrupt: {name}")


def _schema_branch(schema: dict) -> dict:
    for branch in schema.get("anyOf") or []:
        if isinstance(branch, dict) and branch.get("type") != "null":
            return branch
    return schema


def _mock_parameter_value(parameter: dict):
    name = str(parameter.get("name") or "")
    if name in MOCK_TOOL_VALUES:
        return MOCK_TOOL_VALUES[name]
    schema = _schema_branch(parameter.get("value_schema") or {})
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "string":
        return "mock"
    if kind in ("integer", "number"):
        return 1
    if kind == "boolean":
        return True
    if kind == "array":
        return ["mock"]
    raise RuntimeError(f"Provide TOOL_INPUT_VALUES for {name}")


def _tool_input_submission(args: dict) -> dict:
    """Submit the form as the v2 UI does with its initial field modes."""
    inputs = []
    for tool in args.get("tool_inputs") or []:
        for parameter in tool.get("parameters") or []:
            if not parameter.get("editable"):
                continue
            address = {
                "step_index": tool["step_index"],
                "tool_index": tool["tool_index"],
                "parameter": parameter["name"],
            }
            binding = parameter.get("binding")
            if binding:
                reference = {k: binding[k] for k in ("step_index", "tool_index", "output")}
                offered = [
                    {k: option[k] for k in ("step_index", "tool_index", "output")}
                    for option in parameter.get("output_options") or []
                ]
                # Omitting a bound parameter keeps the connection.
                if reference in offered:
                    inputs.append({**address, "source": "tool_output", "reference": reference})
                continue
            if parameter.get("has_value"):
                inputs.append({**address, "source": "value", "value": parameter.get("value")})
            elif parameter.get("required"):
                inputs.append(
                    {**address, "source": "value", "value": _mock_parameter_value(parameter)}
                )
            elif parameter.get("has_default"):
                inputs.append({**address, "source": "default"})
    return {
        "intent": "submit_inputs",
        "workflow_id": args["workflow_id"],
        "workflow_revision": args["workflow_revision"],
        "inputs": inputs,
    }


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
    if REPORT_PATH:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    slowest = report["nodes"][0] if report["nodes"] else {}
    if SUMMARY_PATH:
        summary_path = SUMMARY_PATH
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

    if not NODE_SUMMARY_PATH:
        return
    node_summary_path = NODE_SUMMARY_PATH
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


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _run_sweep() -> int:
    global API_PREFIX, WORKFLOW_RUN_PATH, MOCK_LLM_SLEEP_SECONDS, PROMPT, MAX_HITL_STEPS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--api-prefix", default=API_PREFIX)
    parser.add_argument("--users", type=_positive_int, nargs="+", default=USERS,
                        help="one or more concurrent user counts, e.g. --users 20 or --users 1 5 10")
    parser.add_argument("--repeats", type=_positive_int, nargs="+", default=REPEATS,
                        help="one or more task counts per user, e.g. --repeats 3")
    parser.add_argument("--spawn-rate", type=_positive_int, default=SPAWN_RATE)
    parser.add_argument("--mock-llm-sleep", default=MOCK_LLM_SLEEP_SECONDS)
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--max-hitl-steps", type=_positive_int, default=MAX_HITL_STEPS)
    parser.add_argument("--tool-values", type=json.loads, default={},
                        help='JSON overriding MOCK_TOOL_VALUES, e.g. \'{"lot_cd": "LOT2"}\'')
    parser.add_argument("--out-dir", default=OUT_DIR)
    args = parser.parse_args()

    API_PREFIX = args.api_prefix.rstrip("/")
    WORKFLOW_RUN_PATH = f"{API_PREFIX}/workflow_run"
    MOCK_LLM_SLEEP_SECONDS = str(args.mock_llm_sleep)
    PROMPT = args.prompt
    MAX_HITL_STEPS = args.max_hitl_steps
    MOCK_TOOL_VALUES.update(args.tool_values)

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
    repeats: int,
    timeout_seconds: float,
    report_path: Path,
    summary_path: Path,
    node_summary_path: Path,
) -> None:
    global REPEATS_PER_USER, COMPLETION_TIMEOUT, REPORT_PATH, SUMMARY_PATH, NODE_SUMMARY_PATH

    REPEATS_PER_USER = repeats
    COMPLETION_TIMEOUT = float(timeout_seconds)
    REPORT_PATH = report_path
    SUMMARY_PATH = summary_path
    NODE_SUMMARY_PATH = node_summary_path


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

    environment = Environment(user_classes=[WorkflowRunTimingUser], host=host)
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
