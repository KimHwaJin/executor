"""Check deployed PostgreSQL/Redis using isolated real application paths.

Run from the repository: uv run python scripts/backend_compatibility_check.py
Never upgrades the service schema or starts a runtime. See the companion guide.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from backend_compatibility_flow import FlowFailure, lifecycle_check
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import AuthenticationError, NoPermissionError
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.migrations import upgrade_database
from executor_service.infrastructure.redis_client import create_redis_client
from executor_service.infrastructure.redis_streams import (
    check_redis_compatibility,
    claim_pending,
    trim_before,
)
from executor_service.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")


class CheckFailure(Exception):
    """Only fixed, deliberately public diagnostic messages belong here."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def schema_identifier(value: str) -> str:
    if not SCHEMA_PATTERN.fullmatch(value):
        raise CheckFailure("Schema must be a simple lowercase SQL identifier.")
    return f'"{value}"'


def safe_error(exc: BaseException) -> str:
    """Never echo driver messages, SQL, connection strings or credentials."""
    if isinstance(exc, (CheckFailure, FlowFailure)):
        return str(exc)
    if isinstance(exc, NoPermissionError):
        return (
            "Redis ACL denied a command or test key (including Lua commands)."
        )
    if isinstance(exc, AuthenticationError):
        return "Redis authentication failed; check URL username/password."
    original = getattr(exc, "orig", exc)
    state = getattr(original, "sqlstate", None)
    code = (
        f" SQLSTATE={state}"
        if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state)
        else ""
    )
    hint = {
        "42501": "PostgreSQL permission denied; check schema/DDL permissions.",
        "28P01": "PostgreSQL authentication failed.",
        "3D000": "The PostgreSQL database does not exist.",
        "42P01": "A required PostgreSQL table is missing.",
        "42703": "A required PostgreSQL column is missing.",
        "55P03": "PostgreSQL lock timeout; avoid concurrent migrations.",
        "57014": "PostgreSQL statement timeout/cancellation.",
    }.get(state, "See this stage's guide for checks.")
    return f"{type(exc).__name__}{code}; {hint}"


@dataclass
class Report:
    schema: str
    redis_prefix: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    async def check(
        self,
        name: str,
        action: Callable[[], Awaitable[Any]],
        budget_seconds: float,
    ) -> Any:
        started = time.monotonic()
        print(f"[RUN ] {name}", flush=True)
        try:
            async with asyncio.timeout(budget_seconds):
                result = await action()
        except BaseException as exc:
            detail = safe_error(exc)
            self.checks.append(
                {"name": name, "status": "FAIL", "detail": detail}
            )
            print(f"[FAIL] {name}: {detail}", flush=True)
            raise
        self.checks.append(
            {
                "name": name,
                "status": "PASS",
                "seconds": round(time.monotonic() - started, 3),
            }
        )
        print(f"[PASS] {name}", flush=True)
        return result


def database_engine(dsn: str, schema: str) -> AsyncEngine:
    schema_identifier(schema)
    url = make_url(dsn)
    require(
        url.drivername in {"postgresql", "postgresql+psycopg"},
        "DATABASE_URL must use PostgreSQL with psycopg.",
    )
    # Drop only connection-level options: they could override our isolation.
    url = url.set(drivername="postgresql+psycopg").difference_update_query(
        ["options"]
    )
    return create_async_engine(
        url,
        hide_parameters=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=5,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "options": (
                f"-c search_path={schema} "
                "-c statement_timeout=30000 -c lock_timeout=5000"
            ),
        },
    )


def inspect_schema(connection: Connection, schema: str) -> str:
    """Explicitly qualified, read-only check; never migrate this connection."""
    from executor_service.infrastructure.db import models  # noqa: F401

    inspector = inspect(connection)
    tables = set(inspector.get_table_names(schema=schema))
    required = set(Base.metadata.tables) | {"alembic_version"}
    missing = sorted(required - tables)
    require(not missing, f"Missing service tables: {', '.join(missing)}")
    for name, table in Base.metadata.tables.items():
        columns = {
            column["name"]
            for column in inspector.get_columns(name, schema=schema)
        }
        missing_columns = sorted(set(table.columns.keys()) - columns)
        require(
            not missing_columns,
            f"Missing columns in {name}: {', '.join(missing_columns)}",
        )
    revisions = (
        connection.execute(
            text(
                f"SELECT version_num FROM {schema_identifier(schema)}."
                "alembic_version"
            )
        )
        .scalars()
        .all()
    )
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    displayed = ",".join(
        value
        if isinstance(value, str)
        and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", value)
        else "unknown"
        for value in revisions
    )
    require(
        revisions == [head],
        f"Service revision {displayed or 'empty'} differs from HEAD {head}.",
    )
    return str(head)


async def redis_checks(redis: Redis, prefix: str) -> str:
    """Execute the production compatibility, recovery and retention helpers."""
    await redis.ping()
    await check_redis_compatibility(redis)
    stream = f"{prefix}:recovery"
    await redis.xgroup_create(stream, "workers", id="0", mkstream=True)
    first = await redis.xadd(stream, {"probe": "한국어-output"})
    missing = await redis.xadd(stream, {"probe": "missing"})
    unread = await redis.xadd(stream, {"probe": "unread"})
    await redis.xreadgroup("workers", "old", {stream: ">"}, count=2)
    # Deterministic idle state, without a sleep or server-clock assumption.
    await redis.xclaim(
        stream, "workers", "old", 0, [first, missing], idle=60000
    )
    await redis.xdel(stream, missing)
    recovered = await claim_pending(
        redis, stream, "workers", "new", min_idle_ms=1, count=10
    )
    require(
        recovered.messages == [(first, {"probe": "한국어-output"})]
        and recovered.deleted_ids == [missing],
        "Pending reclaim or missing-body acknowledgement did not match.",
    )
    require(
        await trim_before(redis, stream, unread, protect_groups=True, count=10)
        == 0,
        "Retention removed pending work.",
    )
    await redis.xack(stream, "workers", first)
    require(
        await trim_before(redis, stream, unread, protect_groups=True, count=10)
        == 1,
        "Retention did not remove acknowledged work.",
    )
    require(
        await trim_before(
            redis,
            stream,
            "18446744073709551615-0",
            protect_groups=True,
            count=10,
        )
        == 0,
        "Retention removed unread work.",
    )
    require(
        await trim_before(
            redis,
            stream,
            "18446744073709551615-0",
            protect_groups=False,
            count=10,
        )
        == 1,
        "Event/DLQ retention did not remove expired data.",
    )
    return str((await redis.info("server"))["redis_version"])


def isolated_settings(
    env_file: Path, prefix: str, directory: Path
) -> Settings:
    # Keep the deployed connection settings; override all test-owned paths.
    settings = Settings(
        _env_file=env_file,
        db_migrations_path=ROOT / "migrations",
        db_migration_lock_timeout_seconds=5,
        db_migration_statement_timeout_seconds=30,
        runtime_enabled=False,
        runtime_credential_key=SecretStr(
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        ),
        runtime_allowed_profiles=("default",),
        shared_storage_root=directory,
        redis_work_stream=f"{prefix}:work",
        redis_event_stream=f"{prefix}:events",
        redis_work_dead_letter_stream=f"{prefix}:dlq",
        redis_event_dead_letter_stream=f"{prefix}:events-dlq",
        execution_consumer_group="compatibility-workers",
    )
    # No implicit localhost defaults for a deployment diagnostic.
    import os

    from dotenv import dotenv_values

    values = {
        key.lower(): value for key, value in dotenv_values(env_file).items()
    }
    values.update({key.lower(): value for key, value in os.environ.items()})
    require(
        bool(values.get("database_url")) and bool(values.get("redis_url")),
        "Set DATABASE_URL and REDIS_URL explicitly in environment or env file.",
    )
    return settings


async def run_check(args: argparse.Namespace, report: Report) -> bool:
    created = False
    cleanup_ok = True
    service_engine = None
    test_engine = None
    redis = None
    successful = False
    # Includes only exact run-owned keys, never a scan/glob or FLUSH command.
    keys = [
        f"{report.redis_prefix}:{suffix}"
        for suffix in ("recovery", "work", "events", "dlq", "events-dlq")
    ]
    redis_owned = False
    with tempfile.TemporaryDirectory(prefix="executor-compat-") as temp:
        try:
            settings = isolated_settings(
                args.env_file, report.redis_prefix, Path(temp)
            )
            service_engine = database_engine(
                settings.database_dsn, args.service_schema
            )
            test_engine = database_engine(settings.database_dsn, report.schema)
            redis = create_redis_client(settings)

            async def service_schema() -> None:
                async with service_engine.connect() as connection:
                    # Enforce read-only, not just a convention in Python.
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                    report.evidence[
                        "postgresql_version"
                    ] = await connection.scalar(text("SHOW server_version"))
                    report.evidence[
                        "service_revision"
                    ] = await connection.run_sync(
                        inspect_schema, args.service_schema
                    )

            await report.check(
                "service schema (read-only)", service_schema, 60
            )

            async def redis_probe() -> None:
                nonlocal redis_owned
                require(
                    not await redis.exists(*keys),
                    "Generated Redis keys already exist; refusing to reuse.",
                )
                redis_owned = True
                report.evidence["redis_version"] = await redis_checks(
                    redis, report.redis_prefix
                )

            await report.check(
                "Redis commands / Lua / recovery / retention", redis_probe, 60
            )

            async def migrate() -> None:
                nonlocal created
                async with service_engine.begin() as connection:
                    await connection.execute(
                        text(
                            f"CREATE SCHEMA {schema_identifier(report.schema)}"
                        )
                    )
                created = True
                async with test_engine.connect() as connection:
                    require(
                        await connection.scalar(
                            text("SELECT current_schema()")
                        )
                        == report.schema,
                        "Test schema isolation check failed.",
                    )
                await upgrade_database(test_engine, settings)
                async with test_engine.connect() as connection:
                    await connection.run_sync(inspect_schema, report.schema)

            await report.check(
                "isolated schema / real Alembic migration", migrate, 120
            )

            async def lifecycle() -> None:
                report.evidence.update(
                    await lifecycle_check(test_engine, redis, settings)
                )

            await report.check(
                "submit / Outbox / consume / cancel / events", lifecycle, 120
            )
            successful = True
        except Exception as exc:
            if not report.checks or report.checks[-1]["status"] != "FAIL":
                report.checks.append(
                    {
                        "name": "configuration",
                        "status": "FAIL",
                        "detail": safe_error(exc),
                    }
                )
                print(f"[FAIL] configuration: {safe_error(exc)}", flush=True)
        finally:

            async def cleanup_redis() -> None:
                if redis is not None and redis_owned:
                    await redis.delete(*keys)
                    require(
                        not await redis.exists(*keys),
                        "Test Redis keys remain after cleanup.",
                    )

            async def cleanup_database() -> None:
                if test_engine is not None:
                    await test_engine.dispose()
                if created and service_engine is not None:
                    async with service_engine.begin() as connection:
                        # UUID identifier is generated here, never user input.
                        await connection.execute(
                            text(
                                f"DROP SCHEMA {schema_identifier(report.schema)} "
                                "CASCADE"
                            )
                        )

            for name, action in (
                ("cleanup Redis test keys", cleanup_redis),
                ("cleanup PostgreSQL test schema", cleanup_database),
            ):
                try:
                    await report.check(name, action, 30)
                except Exception:
                    cleanup_ok = False
            if redis is not None:
                await redis.aclose()
            if service_engine is not None:
                await service_engine.dispose()
    return successful and cleanup_ok


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env-file", type=Path, default=ROOT / ".env")
    result.add_argument("--service-schema", default="public")
    result.add_argument("--redis-prefix", default="executor:compatibility")
    result.add_argument(
        "--report",
        type=Path,
        help="Optional JSON report (contains no credentials).",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        schema_identifier(args.service_schema)
        require(
            bool(re.fullmatch(r"[a-zA-Z0-9:_-]{1,128}", args.redis_prefix)),
            "Redis prefix must contain only letters, digits, : _ or -.",
        )
    except CheckFailure as exc:
        print(f"[FAIL] {exc}")
        return 1
    unique = uuid4().hex
    report = Report(
        f"executor_probe_{unique}", f"{args.redis_prefix}:{unique}"
    )
    print(
        f"Test schema: {report.schema}\nTest Redis prefix: {report.redis_prefix}"
    )
    # Application logger.exception() could otherwise print raw driver errors.
    logging.disable(logging.CRITICAL)
    try:
        passed = asyncio.run(run_check(args, report))
    except (Exception, KeyboardInterrupt) as exc:
        passed = False
        print(f"[FAIL] interrupted/closing: {safe_error(exc)}")
    status = "PASS" if passed else "FAIL"
    if args.report:
        try:
            args.report.write_text(
                json.dumps(
                    {"status": status, **asdict(report)},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            print("[FAIL] Cannot write report file.")
            return 1
    print(f"\n{status}: PostgreSQL / Redis compatibility check")
    print(
        "Scope: isolated application lifecycle; NOT Jupyter execution, "
        "load testing or a production-readiness guarantee."
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
