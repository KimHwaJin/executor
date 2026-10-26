"""시나리오 2. 모델 없이 /chat/completion 한 사이클의 처리량만 본다.

Agent 자리에 mock이 정해진 HITL 응답만 돌려준다. 각 단계마다 비동기 sleep을 걸고,
한 사이클 시간은 그 sleep을 뺀 값으로 기록한다.

    MOCK_SLEEP_SECONDS=5 CYCLE_USERS=100 python tests/load/mock_completion_cycle.py

Locust는 tests/load/locust_mock_cycle.py 를 사용한다. mock 서버를 먼저 띄운다.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Response

SLEEP_SECONDS = float(os.environ.get("MOCK_SLEEP_SECONDS", "5"))
USERS = int(os.environ.get("CYCLE_USERS", "100"))

PROMPT = "선택한 데이터의 품질을 분석해줘"
APPROVE = {"decisions": [{"type": "approve"}]}
STEPS = (
    {"kind": "message", "content_text": PROMPT, "status": "interrupted", "name": "data_selection"},
    {"kind": "resume", "command": "mock", "status": "interrupted", "name": "analysis_context"},
    {"kind": "resume", "command": APPROVE, "status": "interrupted", "name": "workflow_candidate_selection"},
    {"kind": "resume", "command": APPROVE, "status": "interrupted", "name": "workflow_approval"},
    {"kind": "resume", "command": APPROVE, "status": "running", "name": "EXECUTOR_EVENT"},
)


@dataclass
class CycleResult:
    ok: bool
    requests: int
    overhead_seconds: float
    error: str = ""


@dataclass
class LoadSummary:
    users: int
    cycles_ok: int
    cycles_fail: int
    requests_ok: int
    wall_seconds: float
    overhead_seconds: list[float]

    @property
    def mean_overhead(self) -> float:
        if not self.overhead_seconds:
            return 0.0
        return sum(self.overhead_seconds) / len(self.overhead_seconds)


def _interrupt(name: str) -> list[dict]:
    if name == "EXECUTOR_EVENT":
        return [{"kind": "EXECUTOR_EVENT", "action_requests": [], "review_configs": []}]
    return [{
        "action_requests": [{"name": name, "args": {}, "description": name}],
        "review_configs": [],
    }]


def build_mock_app(sleep_seconds: float) -> FastAPI:
    """Agent 호출을 대체한다. 시나리오 단계의 지정 응답만 반환한다."""
    sessions: dict[str, dict] = {}
    guard = asyncio.Lock()
    app = FastAPI()

    @app.post("/chat/completion")
    async def chat_completion(
        payload: dict,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    ):
        if not x_user_id or not x_user_id.strip():
            raise HTTPException(401, "X-User-ID is required.")
        if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 255:
            raise HTTPException(422, "Idempotency-Key must contain 1–255 nonblank characters.")
        session_id = str(payload.get("session_id") or "")
        async with guard:
            state = sessions.setdefault(session_id, {"step": 0, "run_id": None, "task_id": str(uuid4())})
            step_index = state["step"]
        if step_index >= len(STEPS):
            raise HTTPException(409, "Scenario cycle is already complete.")
        expected = STEPS[step_index]
        if payload.get("type") != expected["kind"]:
            raise HTTPException(409, "Unexpected scenario step.")
        if expected["kind"] == "message":
            if payload.get("content_text") != expected["content_text"]:
                raise HTTPException(409, "Unexpected message.")
        else:
            if payload.get("run_id") != state["run_id"] or payload.get("command") != expected["command"]:
                raise HTTPException(409, "Unexpected resume.")
        started = time.perf_counter()
        await asyncio.sleep(sleep_seconds)
        slept = time.perf_counter() - started
        run_id = str(uuid4())
        async with guard:
            current = sessions[session_id]
            if current["step"] != step_index:
                raise HTTPException(409, "Concurrent step on the same session.")
            current["step"] = step_index + 1
            current["run_id"] = run_id
            task_id = current["task_id"]
        response.headers["X-Mock-Sleep-Seconds"] = f"{slept:.6f}"
        response.status_code = 202
        return {
            "id": run_id,
            "session_id": session_id,
            "task_id": task_id,
            "status": expected["status"],
            "interrupt": _interrupt(expected["name"]),
        }

    return app


app = build_mock_app(SLEEP_SECONDS)


def _matches(body: dict, expected: dict) -> bool:
    interrupt = (body.get("interrupt") or [{}])[0]
    if body.get("status") != expected["status"]:
        return False
    if expected["name"] == "EXECUTOR_EVENT":
        return interrupt.get("kind") == "EXECUTOR_EVENT"
    action = (interrupt.get("action_requests") or [{}])[0]
    return action.get("name") == expected["name"]


async def one_cycle(client: httpx.AsyncClient) -> CycleResult:
    session_id = str(uuid4())
    headers = {"X-User-ID": "mock-user"}
    run_id = None
    started = time.perf_counter()
    slept = 0.0
    try:
        for expected in STEPS:
            if expected["kind"] == "message":
                payload = {"type": "message", "session_id": session_id, "content_text": expected["content_text"]}
            else:
                payload = {
                    "type": "resume", "session_id": session_id,
                    "run_id": run_id, "command": expected["command"],
                }
            response = await client.post(
                "/chat/completion", json=payload,
                headers={**headers, "Idempotency-Key": uuid4().hex},
            )
            if response.status_code != 202 or not _matches(response.json(), expected):
                return CycleResult(False, 0, 0.0, f"HTTP {response.status_code}: {response.text[:200]}")
            slept += float(response.headers.get("X-Mock-Sleep-Seconds", "0"))
            run_id = response.json()["id"]
    except Exception as exc:
        return CycleResult(False, 0, 0.0, str(exc))
    overhead = (time.perf_counter() - started) - slept
    return CycleResult(True, len(STEPS), overhead)


async def run_load(users: int, sleep_seconds: float) -> LoadSummary:
    app = build_mock_app(sleep_seconds)
    timeout = httpx.Timeout(max(30.0, sleep_seconds * len(STEPS) + 30.0))
    started = time.perf_counter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock", timeout=timeout,
    ) as client:
        results = await asyncio.gather(*(one_cycle(client) for _ in range(users)))
    ok = [item for item in results if item.ok]
    return LoadSummary(
        users=users,
        cycles_ok=len(ok),
        cycles_fail=len(results) - len(ok),
        requests_ok=sum(item.requests for item in ok),
        wall_seconds=time.perf_counter() - started,
        overhead_seconds=[item.overhead_seconds for item in ok],
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def format_summary(summary: LoadSummary) -> str:
    overhead_ms = [value * 1000 for value in summary.overhead_seconds]
    return "\n".join([
        f"cycles_ok={summary.cycles_ok}",
        f"cycles_fail={summary.cycles_fail}",
        f"requests_ok={summary.requests_ok}",
        f"wall_seconds={summary.wall_seconds:.3f}",
        f"cycle_overhead_excluding_sleep_ms mean={summary.mean_overhead * 1000:.1f} "
        f"p50={_percentile(overhead_ms, 0.50):.1f} p95={_percentile(overhead_ms, 0.95):.1f}",
    ])


def main() -> None:
    summary = asyncio.run(run_load(USERS, SLEEP_SECONDS))
    print(format_summary(summary))
    if summary.cycles_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
