# Quality Gates

Executor uses product-independent commands so the same checks can run locally,
in the internal CI/CD platform, or in a future hosted CI service. A successful
image build alone is not a quality gate.

## Pull-request gate

Run the deterministic static and unit-test phase on every change:

```bash
uv run python scripts/quality_gate.py
```

It requires all of the following to pass:

- Ruff lint;
- Ruff format under the configured preferred 79-character line length;
- ty static type checking; and
- tests that do not require external PostgreSQL or Redis services.

## PostgreSQL and Redis integration gate

Provide a PostgreSQL role that can create and drop disposable databases and a
dedicated Redis test database, then run:

```bash
export EXECUTOR_POSTGRES_TEST_ADMIN_URL=\
'postgresql+psycopg://executor:executor@127.0.0.1:5432/postgres'
export EXECUTOR_REDIS_TEST_URL='redis://127.0.0.1:6379/15'
uv run python scripts/quality_gate.py --integration
```

PowerShell uses the same command after setting environment variables:

```powershell
$env:EXECUTOR_POSTGRES_TEST_ADMIN_URL = `
  'postgresql+psycopg://executor:executor@127.0.0.1:5432/postgres'
$env:EXECUTOR_REDIS_TEST_URL = 'redis://127.0.0.1:6379/15'
uv run python scripts/quality_gate.py --integration
```

The integration phase fails instead of skipping when Redis is unavailable.
The PostgreSQL suite creates a fresh database per test, applies Alembic to
`head`, runs `alembic check`, exercises concurrent Workers, and drops the test
database afterward. Never point the admin URL at a production server.

## Ordered Outbox load gate

이벤트 순서 보장 쿼리 또는 Outbox 인덱스를 변경할 때는 disposable PostgreSQL DB와
전용 Redis test DB에서 다음 부하 검사를 실행한다.

```bash
uv run python scripts/outbox_ordering_load_smoke.py
```

기본 시나리오는 다음 두 가지다.

- 단일 Execution에 미발행 이벤트 2,000개가 적체된 상황
- 30개 Execution에 각각 미발행 이벤트 100개가 적체된 상황

각 실행은 Execution별 `event_sequence`가 빠짐없이 오름차순으로 발행되는지 검증하고,
처리 시간·초당 이벤트 수·Publisher 반복 횟수를 출력한다. 필요하면
`--min-events-per-second`로 해당 실행환경의 회귀 하한을 지정한다. 스크립트는 테스트마다
새 PostgreSQL DB를 생성하고 제거하므로 운영 DB 권한이나 주소를 사용하면 안 된다.

```bash
uv run python scripts/outbox_ordering_load_smoke.py \
  --single-events 2000 \
  --parallel-executions 30 \
  --parallel-events 100 \
  --min-events-per-second 100
```

2026-08-26 로컬 Docker 기준 참고 결과는 단일 Execution 2,000개가 21 Publisher
rounds, 1.66초, 약 1,203 events/s였고, 30개 Execution의 총 3,000개가 21 rounds,
1.91초, 약 1,568 events/s였다. 이 값은 운영환경 SLA가 아니라 동일 장비에서 회귀를
탐지하기 위한 기준선이다.

## Runtime release gate

Real Jupyter execution is intentionally separate from the fast pull-request
gate. Start the local test topology described in
[Executor Resilience Testing](executor-resilience-testing.md), then run:

```bash
uv run python scripts/local_validation_suite.py --full
```

Add `--include-faults` for disruptive Executor, Redis, and Jupyter outage
scenarios. Long soak durations remain a scheduled or release-candidate gate
rather than running for every commit.

## Required pipeline behavior

- A failed command blocks merge or deployment.
- Integration jobs must provision real PostgreSQL and Redis services and may
  not report skipped required tests as success.
- Runtime release evidence retains the generated `test-results` summary and
  logs.
- Secrets are injected by the CI platform and are never committed or printed.
- Production-readiness changes add their concurrency, recovery, and retention
  cases to the integration or runtime gate before completion.

Ruff's formatter can intentionally retain an overlong string literal, URL, or
docstring when splitting it would change its value or harm readability. For
that reason `E501` is excluded from lint while `line-length = 79` remains the
repository-wide formatting target.
