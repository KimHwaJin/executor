"""Local Redis compatibility E2E with a fresh DB and separate Executor process.

Requires existing local PostgreSQL, dedicated Redis, and Jupyter extension.
Never resets existing databases, Streams, or servers. Generated result files
and the process log are retained in a printed temporary directory for review.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet
from mcp import Client
from psycopg import sql
from redis.asyncio import Redis
from resilience_common import (
    available_port,
    stop_executor,
    upsert_runtime_target,
    wait_ready,
)
from sqlalchemy.engine import make_url

from executor_service.infrastructure.redis_streams import (
    check_redis_compatibility,
)
from executor_service.settings import get_settings

ROOT = Path(__file__).resolve().parents[1]


def _database(admin_url: str, name: str, *, create: bool) -> None:
    if not name.startswith("executor_redis_compat_"):
        raise ValueError(
            "Only disposable compatibility databases are allowed."
        )
    with psycopg.connect(admin_url, autocommit=True) as connection:
        statement = (
            "CREATE DATABASE {}" if create else "DROP DATABASE {} WITH (FORCE)"
        )
        connection.execute(sql.SQL(statement).format(sql.Identifier(name)))


async def main(
    *,
    smoke_scripts: tuple[str, ...] = (
        "single_failure_retry_cancel_e2e.py",
        "multi_execution_lifecycle_e2e.py",
    ),
) -> None:
    settings = get_settings()
    redis_url = os.environ["EXECUTOR_REDIS_TEST_URL"]
    database_url = make_url(settings.database_dsn)
    if database_url.host not in {"localhost", "127.0.0.1"} or make_url(
        redis_url
    ).host not in {"localhost", "127.0.0.1"}:
        raise ValueError(
            "This smoke test is restricted to local DB and Redis servers."
        )
    token = os.environ["REDIS_COMPAT_JUPYTER_TOKEN"]
    endpoint = os.getenv(
        "REDIS_COMPAT_JUPYTER_ENDPOINT", "http://127.0.0.1:8888"
    )
    profile = os.getenv("REDIS_COMPAT_RUNTIME_PROFILE", "default")
    redis = Redis.from_url(redis_url, decode_responses=True)
    process = None
    created = False
    unique = uuid4().hex
    database_name = f"executor_redis_compat_{unique}"
    admin_url = database_url.set(
        drivername="postgresql", database="postgres"
    ).render_as_string(hide_password=False)
    stream = f"test:compat:e2e:{unique}"
    directory = Path(tempfile.mkdtemp(prefix="executor-redis-compat-"))
    port = available_port("REDIS_COMPAT_EXECUTOR_PORT")
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": database_url.set(
                database=database_name
            ).render_as_string(hide_password=False),
            "REDIS_URL": redis_url,
            "DB_AUTO_MIGRATE": "true",
            "SHARED_STORAGE_ROOT": str(directory / "shared"),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "RUNTIME_ENABLED": "true",
            "RUNTIME_ALLOWED_PROFILES": profile,
            "RUNTIME_CREDENTIAL_KEY": Fernet.generate_key().decode(),
            "REDIS_WORK_STREAM": stream,
            "REDIS_EVENT_STREAM": f"{stream}.events",
            "REDIS_WORK_DEAD_LETTER_STREAM": f"{stream}.dlq",
            "REDIS_EVENT_DEAD_LETTER_STREAM": f"{stream}.events.dlq",
            "EXECUTION_CONSUMER_NAME": f"compat-{unique}",
            "EXECUTION_CONSUMER_GROUP": f"compat-{unique}",
            "EXECUTOR_MCP_URL": f"http://127.0.0.1:{port}/mcp",
            "EXECUTOR_REST_URL": f"http://127.0.0.1:{port}/api/v1",
            "MCP_ALLOWED_HOSTS": f"127.0.0.1:{port},localhost:{port}",
            "MCP_ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
            "SINGLE_LIFECYCLE_RUNTIME_PROFILE": profile,
            "MULTI_LIFECYCLE_RUNTIME_PROFILE": profile,
            "SINGLE_LIFECYCLE_JUPYTER_ENDPOINT": endpoint,
            "MULTI_LIFECYCLE_JUPYTER_ENDPOINT": endpoint,
        }
    )
    print(f"Test evidence: {directory}", flush=True)
    try:
        await check_redis_compatibility(redis)
        print(
            "Redis server:",
            (await redis.info("server"))["redis_version"],
            flush=True,
        )
        await asyncio.to_thread(
            _database, admin_url, database_name, create=True
        )
        created = True
        with (directory / "executor.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "from executor_service.main import run; run()",
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
            )
        await wait_ready(port)
        async with Client(environment["EXECUTOR_MCP_URL"]) as client:
            await upsert_runtime_target(
                client,
                unique=unique,
                name="local-jupyter",
                endpoint=endpoint,
                pool="INTERACTIVE",
                token=token,
            )
        for script in smoke_scripts:
            await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(ROOT / "scripts" / script)],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        print("Redis compatibility SINGLE/MULTI E2E passed.", flush=True)
    finally:
        try:
            await stop_executor(process)
        finally:
            try:
                await redis.delete(
                    stream,
                    f"{stream}.events",
                    f"{stream}.dlq",
                    f"{stream}.events.dlq",
                )
            finally:
                try:
                    await redis.aclose()
                finally:
                    if created:
                        await asyncio.to_thread(
                            _database, admin_url, database_name, create=False
                        )


if __name__ == "__main__":
    from executor_service.event_loop import run_async

    run_async(main())
