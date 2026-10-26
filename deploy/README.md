# Gaia 배포 준비

이 디렉터리는 하나의 `dtest-agent` 이미지로 두 프로세스를 배포한다.

- `dtest-agent-api`: FastAPI, 테스트 HTML, CRUD Run queue와 LangGraph 실행
- `dtest-agent-worker`: Redis Executor event consumer와 LangGraph resume

배포 전에 `dtest-agent.yaml`의 다음 환경별 값을 Gaia 리소스에 맞춘다.

1. 모든 `image: dtest-agent:latest`를 사내 registry 이미지로 교체한다.
2. `dtest-shared-pv`를 Agent와 Executor가 함께 마운트하는 PVC 이름으로 교체한다.
3. `secret.example.yaml`을 복사해 실제 Secret 관리 절차로 `dtest-agent-secrets`를 만든다. 예시 파일에 실제 비밀번호를 저장하거나 commit하지 않는다.
4. API의 `CHECKPOINT_DB_URI`와 Worker의 `AGENT_CHECKPOINT_DATABASE_URL`은 같은 LangGraph checkpoint DB를 가리켜야 한다.
5. `EW_DATABASE_URL`에는 Worker migration과 `ew_*` 테이블을 둘 PostgreSQL을 지정한다.

두 migration은 각 Deployment의 init container에서 실행된다. PostgreSQL advisory lock과 Alembic revision으로 중복 실행을 직렬화한다.

```bash
docker build -t dtest-agent:local .
docker compose up
```

```bash
kubectl apply -f deploy/dtest-agent.yaml
```

확인 endpoint:

- API: `GET /health`, `GET /demo`
- Worker: `GET :8011/health/live`, `GET :8011/health/ready`, `GET :8011/metrics`
