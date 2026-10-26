"""Real HTTP + Worker + LangGraph/HITL load test, using /chat/completion for Agent operations.

Run against an isolated database. Creates one disposable user per simulated user
and one session per conversation. EXECUTOR_SUBMIT_ENABLED=false is suitable for
planning/request-generation tests; it does not measure external execution.
"""
import json
import os
import time
from uuid import uuid4

from locust import HttpUser, between, task
from locust.exception import StopUser


class CompletionUser(HttpUser):
    wait_time = between(0.3, 0.7)

    def call(self, body, *, key=None):
        headers = dict(getattr(self, "auth", {}))
        if key:
            headers["Idempotency-Key"] = key
        kind = body["type"]
        method, path, data = "POST", "/chat/completion", body
        if kind == "signup":
            path, data = "/api/v1/users", body["user"]
        elif kind == "projects":
            method, path, data = "GET", "/api/v1/projects", None
        elif kind == "session_create":
            path, data = f"/api/v1/projects/{body['project_id']}/sessions", {}
        elif kind == "messages":
            method, path, data = "GET", f"/api/v1/sessions/{body['session_id']}/messages", None
        with self.client.request(
            method, path, json=data, headers=headers,
            name=f"completion/{body['type']}", catch_response=True, timeout=30,
        ) as response:
            if response.status_code not in (200, 201, 202):
                response.failure(f"HTTP {response.status_code}: {response.text[:300]}")
                raise StopUser()
            try:
                return response.json()
            except ValueError:
                response.failure("Expected JSON")
                raise StopUser()

    def on_start(self):
        self.run = None
        self.started = None
        self.steps = 0
        user = self.call({"type": "signup", "user": {"user_name": "locust-completion-" + uuid4().hex}})
        self.auth = {"Authorization": f"Bearer {user['user_id']}"}
        projects = self.call({"type": "projects"})["items"]
        self.project_id = next((p["id"] for p in projects if p["is_default"]), projects[0]["id"])

    def finish(self, error=None):
        self.environment.events.request.fire(
            request_type="FLOW", name="conversation", response_time=(time.monotonic() - self.started) * 1000,
            response_length=0, exception=RuntimeError(error) if error else None,
        )
        self.started = None
        self.run = None
        if error or os.environ.get("COMPLETION_ONCE") == "1":
            raise StopUser()

    @task
    def conversation(self):
        if self.run is None:
            self.started = time.monotonic()
            self.steps = 0
            self.session_id = self.call({"type": "session_create", "project_id": self.project_id})["id"]
            self.run = self.call({
                "type": "message", "session_id": self.session_id,
                "content_text": os.environ.get("COMPLETION_PROMPT", "공정 데이터와 불량 데이터를 이용해서 불량 예측 분석 계획을 세워줘."),
            }, key=uuid4().hex)
            return
        if time.monotonic() - self.started > float(os.environ.get("COMPLETION_TIMEOUT", "240")):
            self.finish("Conversation timed out")
        self.run = self.call({"type": "status", "session_id": self.session_id, "run_id": self.run["id"]})
        status = self.run["status"]
        if status in ("pending", "running"):
            return
        if status in ("error", "timeout", "canceled"):
            self.finish(f"Unexpected terminal status: {status}: {self.run.get('failure')}")
        if status == "success":
            self.call({"type": "messages", "session_id": self.session_id})
            self.finish()
            return
        if status != "interrupted":
            self.finish(f"Unknown status: {status}")
        self.steps += 1
        if self.steps > 16:
            self.finish("Too many HITL steps")
        item = self.run["interrupt"][0]
        action = (item.get("action_requests") or [{}])[0]
        kind = action.get("name") or item.get("kind")
        commands = {
            "data_selection": "mock",
            "analysis_context": {"objective": "공정 변수와 불량의 관계를 분석하고 주요 영향 변수를 확인한다."},
            "workflow_candidate_selection": {"candidate_number": 1},
            "workflow_approval": {"approved": True},
            "next_user_request": {"action": "cancel", "user_request": None},
        }
        if kind == "additional_information":
            config = (item.get("review_configs") or [{}])[0]
            properties = (config.get("args_schema") or item.get("schema") or {}).get("properties", {})
            defaults = {"join_columns": ["alias_lot_id", "wf_id", "x", "y"], "target_column": "fail", "test_size": 0.2}
            defaults.update(json.loads(os.environ.get("COMPLETION_ANSWERS", "{}")))
            answers = {}
            for name, schema in properties.items():
                value = defaults.get(name, (action.get("args") or {}).get(name))
                if value is None or value == "":
                    value = schema.get("default")
                if value is None:
                    self.finish(f"Provide COMPLETION_ANSWERS for {name}")
                answers[name] = value
            command = {"answers": answers}
        elif kind in commands:
            command = commands[kind]
        else:
            self.finish(f"Unsupported HITL: {kind}")
        self.run = self.call({"type": "resume", "session_id": self.session_id, "run_id": self.run["id"], "command": command}, key=uuid4().hex)

    def on_stop(self):
        # Record incomplete journeys instead of presenting HTTP 202s as E2E success.
        if getattr(self, "started", None) is not None:
            self.environment.events.request.fire(
                request_type="FLOW", name="conversation", response_time=(time.monotonic() - self.started) * 1000,
                response_length=0, exception=RuntimeError("Stopped before conversation completed"),
            )
