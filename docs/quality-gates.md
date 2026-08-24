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
