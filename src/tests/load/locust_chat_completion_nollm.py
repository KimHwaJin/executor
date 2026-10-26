"""시나리오 1. 사용자마다 세션을 반복 생성한다. 모델과 Agent는 호출하지 않는다.

세션 생성은 /chat/completion에 없는 작업이라 기존 세션 API를 사용한다.
시나리오 2는 tests/load/mock_completion_cycle.py 가 담당한다.

    LOAD_SCENARIO=sessions locust -f tests/load/locust_chat_completion_nollm.py \\
      --host http://127.0.0.1:8012 --headless -u 100 -r 20 -t 60s
"""
import os
from uuid import uuid4

from locust import HttpUser, between, task
from locust.exception import StopUser


class SessionCreateUser(HttpUser):
    wait_time = between(0.1, 0.3)

    def on_start(self):
        if os.environ.get("LOAD_SCENARIO", "sessions") != "sessions":
            raise StopUser()
        name = "locust-session-" + uuid4().hex
        with self.client.post(
            "/api/v1/users", json={"user_name": name}, name="session/signup",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"HTTP {response.status_code}: {response.text[:300]}")
                raise StopUser()
            self.auth = {"Authorization": f"Bearer {response.json()['user_id']}"}
        with self.client.get(
            "/api/v1/projects", headers=self.auth, name="session/projects",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:300]}")
                raise StopUser()
            projects = response.json()["items"]
            if not projects:
                response.failure("User has no project")
                raise StopUser()
            self.project_id = next((p["id"] for p in projects if p.get("is_default")), projects[0]["id"])

    @task
    def create_one_session(self):
        with self.client.post(
            f"/api/v1/projects/{self.project_id}/sessions",
            json={},
            headers=self.auth,
            name="session/create",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"HTTP {response.status_code}: {response.text[:300]}")
