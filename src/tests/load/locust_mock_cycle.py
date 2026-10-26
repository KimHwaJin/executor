"""시나리오 2 Locust. mock 서버의 /chat/completion만 호출한다.

서버를 먼저 띄운다.

    MOCK_SLEEP_SECONDS=5 .venv/bin/python -m uvicorn mock_completion_cycle:app \\
      --app-dir tests/load --host 127.0.0.1 --port 8013

    locust -f tests/load/locust_mock_cycle.py --host http://127.0.0.1:8013 \\
      --headless -u 100 -r 20 -t 60s

POST 시간에는 단계 sleep이 포함된다. sleep을 뺀 한 사이클 시간은
FLOW cycle_excluding_sleep 이다.
"""
import time
from uuid import uuid4

from locust import HttpUser, between, task

from mock_completion_cycle import STEPS, _matches


class MockCycleUser(HttpUser):
    wait_time = between(0.1, 0.3)

    def on_start(self):
        self.auth = {"Authorization": "Bearer mock-user"}

    def record_cycle(self, started, slept, error=None):
        self.environment.events.request.fire(
            request_type="FLOW",
            name="cycle_excluding_sleep",
            response_time=max(0.0, time.perf_counter() - started - slept) * 1000,
            response_length=0,
            exception=RuntimeError(error) if error else None,
        )

    @task
    def cycle(self):
        session_id = str(uuid4())
        run_id = None
        started = time.perf_counter()
        slept = 0.0
        for expected in STEPS:
            if expected["kind"] == "message":
                payload = {
                    "type": "message",
                    "session_id": session_id,
                    "content_text": expected["content_text"],
                }
            else:
                payload = {
                    "type": "resume",
                    "session_id": session_id,
                    "run_id": run_id,
                    "command": expected["command"],
                }
            with self.client.post(
                "/chat/completion",
                json=payload,
                headers={**self.auth, "Idempotency-Key": uuid4().hex},
                name=f"completion/{expected['name']}",
                catch_response=True,
                timeout=60,
            ) as response:
                if response.status_code != 202:
                    response.failure(f"HTTP {response.status_code}: {response.text[:200]}")
                    self.record_cycle(started, slept, f"HTTP {response.status_code} at {expected['name']}")
                    return
                body = response.json()
                if not _matches(body, expected):
                    response.failure(f"Unexpected body: {response.text[:200]}")
                    self.record_cycle(started, slept, f"Unexpected body at {expected['name']}")
                    return
                slept += float(response.headers.get("X-Mock-Sleep-Seconds", "0"))
                run_id = body["id"]
        self.record_cycle(started, slept)
