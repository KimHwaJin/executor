"""Load a deployed Executor with MULTI sessions and print its throughput.

Each of K sessions sends ROUNDS Operations of PER Steps, one after the
other, and waits for each to finish before it sends the next. The Steps
print one line, so the result is what Executor itself costs per Step, not
what the code costs.

    EXECUTOR_URL=https://executor.example/api/v1 EXECUTOR_API_KEY=... \
    EXECUTOR_USER_ID=loadtest EXECUTOR_TENANT_ID=... \
    uv run python scripts/multi_load.py K HISTORY PER ROUNDS

    uv run python scripts/multi_load.py 12 30 5 20

HISTORY is how many Steps each session holds before the measurement
starts. Sessions are finalized at the end, which frees their kernels; a
run that is interrupted leaves them for the caller to finalize.

Environment:
    EXECUTOR_URL      comma-separated URLs take the sessions in turn
                      (default http://localhost:8000/api/v1)
    RUNTIME_PROFILE   Jupyter profile name (default "default")
    WAIT_SECONDS      ask status with wait_seconds instead of polling
    POLL_SECONDS      pause between status requests (default 0.5)
    OUT_BYTES         bytes each Step prints (default: one short line)
"""

import itertools
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

URLS = itertools.cycle(
    os.environ.get("EXECUTOR_URL", "http://localhost:8000/api/v1").split(",")
)
HEADERS = {
    "X-Api-Key": os.environ["EXECUTOR_API_KEY"],
    "X-User-Id": os.environ.get("EXECUTOR_USER_ID", "loadtest"),
}
if os.environ.get("EXECUTOR_TENANT_ID"):
    HEADERS["X-Tenant-Id"] = os.environ["EXECUTOR_TENANT_ID"]
PROFILE = os.environ.get("RUNTIME_PROFILE", "default")
WAIT = os.environ.get("WAIT_SECONDS")
POLL = float(os.environ.get("POLL_SECONDS", "0.5"))
OUT = int(os.environ.get("OUT_BYTES", "0"))
K, HISTORY, PER, ROUNDS = (int(value) for value in sys.argv[1:5])


def _code(sequence: int) -> str:
    text = f"print('step', {sequence})"
    if OUT:
        rows = max(1, OUT // 100)
        text += f"\nprint(('row {sequence} ' + 'x' * 90 + chr(10)) * {rows})"
    return text


def _spec(start: int, count: int) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "steps": [
            {
                "sequence": start + offset,
                "payload": {
                    "type": "PYTHON_EXECUTE",
                    "source": {
                        "type": "INLINE",
                        "content": _code(start + offset),
                    },
                },
            }
            for offset in range(count)
        ],
    }


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


class Session:
    def __init__(self) -> None:
        self.http = httpx.Client(
            base_url=next(URLS).rstrip("/"), headers=HEADERS, timeout=120
        )
        self.id = ""
        self.version = 0
        self.steps = 0

    def wait(self, wanted: str = "WAITING_FOR_OPERATION") -> None:
        params = {"wait_seconds": WAIT} if WAIT else None
        while True:
            body = self.http.get(
                f"/executions/{self.id}", params=params
            ).json()
            state = body["state"]
            if state["status"] == wanted:
                self.version = state["version"]
                return
            if state["status"] in ("FAILED", "CANCELLED"):
                raise RuntimeError(str(body)[:600])
            if not WAIT:
                time.sleep(POLL)

    def start(self) -> None:
        response = self.http.post(
            "/executions",
            json={
                "idempotency_key": f"load-{uuid.uuid4()}",
                "lifecycle": {
                    "operation_mode": "MULTI",
                    "operation_wait_timeout_seconds": 3600,
                },
                "trigger": {"type": "INTERACTIVE"},
                "runtime": {"type": "JUPYTER", "profile": PROFILE},
                "operation": {"spec": _spec(0, 1)},
            },
        )
        if response.status_code != 202:
            raise RuntimeError(response.text)
        self.id = response.json()["execution_id"]
        self.steps = 1

    def add(self, count: int) -> float:
        """Send one Operation; the seconds its acceptance took."""

        started = time.time()
        response = self.http.post(
            f"/executions/{self.id}/operations",
            json={
                "idempotency_key": f"op-{uuid.uuid4()}",
                "expected_version": self.version,
                "spec": _spec(self.steps, count),
            },
        )
        if response.status_code != 202:
            raise RuntimeError(response.text)
        self.steps += count
        return time.time() - started

    def finalize(self) -> None:
        self.http.post(
            f"/executions/{self.id}/finalize",
            json={
                "idempotency_key": f"fin-{uuid.uuid4()}",
                "expected_version": self.version,
            },
        )
        self.wait("SUCCEEDED")


def _prepare(_: int) -> Session:
    session = Session()
    session.start()
    session.wait()
    while session.steps < HISTORY:
        session.add(min(50, HISTORY - session.steps))
        session.wait()
    return session


def _run(session: Session) -> list[tuple[float, float]]:
    timings = []
    for _ in range(ROUNDS):
        started = time.time()
        accepted = session.add(PER)
        session.wait()
        timings.append((accepted, time.time() - started))
    return timings


def main() -> None:
    with ThreadPoolExecutor(K) as pool:
        sessions = list(pool.map(_prepare, range(K)))
        print(f"{K} sessions ready, {max(HISTORY, 1)} Steps each", flush=True)
        started = time.time()
        timings = [
            timing for result in pool.map(_run, sessions) for timing in result
        ]
        elapsed = time.time() - started
        accepts = [accepted for accepted, _ in timings]
        dones = [done for _, done in timings]
        print(
            f"{K} sessions x {ROUNDS} Operations x {PER} Steps\n"
            f"  accepted        p50 {_quantile(accepts, 0.5) * 1000:.0f}ms"
            f"  p95 {_quantile(accepts, 0.95) * 1000:.0f}ms\n"
            f"  Operation done  p50 {_quantile(dones, 0.5):.2f}s"
            f"  p95 {_quantile(dones, 0.95):.2f}s\n"
            f"  throughput      {len(timings) * PER / elapsed:.1f} Step/s"
            f" ({elapsed:.0f}s)",
            flush=True,
        )
        list(pool.map(Session.finalize, sessions))
        print("sessions finalized")


if __name__ == "__main__":
    main()
