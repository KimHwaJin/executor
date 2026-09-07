"""Safety and real-backend coverage of the single-command deployment probe."""

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url

SCRIPTS = Path(__file__).parents[1] / "scripts"


def load_probe() -> Any:
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("backend_compatibility_check")
    finally:
        sys.path.remove(str(SCRIPTS))


probe = load_probe()


@pytest.mark.parametrize(
    "name",
    [
        "public;DROP SCHEMA public",
        "public,x",
        '"public"',
        "PUBLIC",
        "a" * 64,
        "",
    ],
)
def test_schema_rejects_sql_and_search_path_injection(name: str) -> None:
    with pytest.raises(probe.CheckFailure):
        probe.schema_identifier(name)


def test_errors_do_not_echo_connection_strings_or_sql_parameters() -> None:
    secret = "redis://private:password@secret-host/0"
    assert secret not in probe.safe_error(RuntimeError(secret))
    assert "password" not in probe.safe_error(ValueError(secret))
    assert (
        probe.safe_error(probe.CheckFailure("Known failure"))
        == "Known failure"
    )


def test_engine_forces_isolated_search_path_and_preserves_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake(url: Any, **kwargs: Any) -> object:
        captured.update(url=url, **kwargs)
        return object()

    monkeypatch.setattr(probe, "create_async_engine", fake)
    probe.database_engine(
        "postgresql+psycopg://user:pw@db/name?sslmode=require"
        "&options=-c%20search_path%3Dpublic",
        "executor_probe_safe",
    )
    assert captured["url"].query == {"sslmode": "require"}
    assert (
        "search_path=executor_probe_safe "
        in captured["connect_args"]["options"]
    )
    assert "public" not in captured["connect_args"]["options"]
    assert captured["hide_parameters"] is True


async def test_stage_failure_is_recorded_without_exception_secrets() -> None:
    report = probe.Report("test", "test")

    async def fail() -> None:
        raise RuntimeError("super-secret-value")

    with pytest.raises(RuntimeError):
        await report.check("failure", fail, 1)
    assert report.checks[0]["status"] == "FAIL"
    assert "super-secret-value" not in str(report.checks)


@pytest_asyncio.fixture
async def deployed_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[Any, Any, Any]]:
    dsn = os.getenv("EXECUTOR_COMPAT_TEST_DATABASE_URL")
    redis_url = os.getenv("EXECUTOR_REDIS_TEST_URL")
    if not dsn or not redis_url:
        # Requires EXECUTOR_COMPAT_TEST_DATABASE_URL and EXECUTOR_REDIS_TEST_URL.
        pytest.skip()
    # A test-only stand-in for an already deployed service schema. Never public.
    schema = f"executor_probe_fixture_{uuid4().hex}"
    prefix = f"executor:compatibility:fixture:{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("REDIS_URL", redis_url)
    settings = probe.isolated_settings(
        tmp_path / "absent.env", prefix, tmp_path
    )
    engine = probe.database_engine(dsn, schema)
    created = False
    redis = probe.create_redis_client(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        await probe.upgrade_database(engine, settings)
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE sentinel (value text)")
            )
            await connection.execute(
                text("INSERT INTO sentinel VALUES ('untouched')")
            )
        yield (
            engine,
            redis,
            argparse.Namespace(
                env_file=tmp_path / "absent.env",
                service_schema=schema,
            ),
        )
    finally:
        if created:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA "{schema}" CASCADE')
                )
        await engine.dispose()
        await redis.aclose()


def new_report() -> Any:
    unique = uuid4().hex
    return probe.Report(
        f"executor_probe_{unique}", f"executor:compatibility:{unique}"
    )


async def verify_cleanup(engine: Any, redis: Any, report: Any) -> None:
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT value FROM sentinel"))
            == "untouched"
        )
        assert (
            await connection.scalar(text("SELECT count(*) FROM executions"))
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_namespace WHERE nspname=:schema"
                ),
                {"schema": report.schema},
            )
            == 0
        )
    # Test-only inspection; the shipped CLI itself never scans Redis keys.
    assert [
        key async for key in redis.scan_iter(f"{report.redis_prefix}:*")
    ] == []


@pytest.mark.postgres
@pytest.mark.redis
async def test_real_backend_round_trip_and_cleanup(
    deployed_schema: Any,
) -> None:
    engine, redis, args = deployed_schema
    report = new_report()
    assert await probe.run_check(args, report)
    assert all(item["status"] == "PASS" for item in report.checks)
    assert report.evidence["execution_status"] == "CANCELLED"
    assert report.evidence["event_types"][-1] == "execution.completed"
    assert report.evidence["jupyter_executed"] is False
    await verify_cleanup(engine, redis, report)


@pytest.mark.postgres
@pytest.mark.redis
async def test_failure_after_writes_still_cleans_up(
    deployed_schema: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, redis, args = deployed_schema
    original = probe.lifecycle_check

    async def fail_after(*values: Any) -> None:
        await original(*values)
        raise RuntimeError("secret-connection-string")

    monkeypatch.setattr(probe, "lifecycle_check", fail_after)
    report = new_report()
    assert not await probe.run_check(args, report)
    assert "secret-connection-string" not in str(report.checks)
    await verify_cleanup(engine, redis, report)


@pytest.mark.postgres
@pytest.mark.redis
async def test_service_revision_mismatch_is_not_migrated(
    deployed_schema: Any,
) -> None:
    engine, redis, args = deployed_schema
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE alembic_version SET version_num='0004'")
        )
    report = new_report()
    assert not await probe.run_check(args, report)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            == "0004"
        )
    await verify_cleanup(engine, redis, report)


@pytest.mark.postgres
@pytest.mark.redis
async def test_missing_column_at_head_is_detected(
    deployed_schema: Any,
) -> None:
    engine, redis, args = deployed_schema
    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE executions DROP COLUMN finished_at")
        )
    report = new_report()
    assert not await probe.run_check(args, report)
    assert "finished_at" in report.checks[0]["detail"]
    await verify_cleanup(engine, redis, report)


@pytest.mark.postgres
@pytest.mark.redis
async def test_cli_single_command_reports_success(
    deployed_schema: Any,
    tmp_path: Path,
) -> None:
    engine, redis, args = deployed_schema
    output_path = tmp_path / "report.json"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPTS / "backend_compatibility_check.py"),
        "--env-file",
        str(args.env_file),
        "--service-schema",
        args.service_schema,
        "--report",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(60):
            stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    result = json.loads(output_path.read_text())
    assert result["status"] == "PASS"
    assert result["evidence"]["redis_version"]
    assert "PASS: PostgreSQL / Redis" in stdout.decode()
    await verify_cleanup(engine, redis, argparse.Namespace(**result))


@pytest.mark.postgres
@pytest.mark.redis
async def test_interruption_after_schema_creation_cleans_up(
    deployed_schema: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, redis, args = deployed_schema

    async def interrupt(*values: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(probe, "lifecycle_check", interrupt)
    report = new_report()
    with pytest.raises(asyncio.CancelledError):
        await probe.run_check(args, report)
    await verify_cleanup(engine, redis, report)


@pytest.mark.postgres
@pytest.mark.redis
async def test_existing_redis_key_is_never_deleted(
    deployed_schema: Any,
) -> None:
    engine, redis, args = deployed_schema
    report = new_report()
    key = f"{report.redis_prefix}:events"
    await redis.xadd(key, {"sentinel": "untouched"})
    try:
        assert not await probe.run_check(args, report)
        assert (await redis.xrange(key))[0][1] == {"sentinel": "untouched"}
    finally:
        await redis.delete(key)
    await verify_cleanup(engine, redis, report)


@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.parametrize("denied", ["eval", "xinfo", "xadd", "del"])
async def test_real_redis_acl_denial_is_a_failure(
    deployed_schema: Any,
    monkeypatch: pytest.MonkeyPatch,
    denied: str,
) -> None:
    if os.getenv("EXECUTOR_COMPAT_TEST_ACL") != "1":
        # Opt in with EXECUTOR_COMPAT_TEST_ACL=1 on local disposable Redis.
        pytest.skip()
    url = make_url(os.environ["EXECUTOR_REDIS_TEST_URL"])
    assert url.host in {"localhost", "127.0.0.1"}, "ACL test is local-only"
    engine, redis, args = deployed_schema
    report = new_report()
    name = f"executor_probe_{uuid4().hex}"
    password = uuid4().hex
    assert await redis.execute_command("ACL", "GETUSER", name) is None
    await redis.execute_command(
        "ACL",
        "SETUSER",
        name,
        "on",
        f">{password}",
        f"~{report.redis_prefix}:*",
        "+@all",
        f"-{denied}",
    )
    try:
        monkeypatch.setenv(
            "REDIS_URL",
            url.set(
                username=name,
                password=password,
            ).render_as_string(hide_password=False),
        )
        assert not await probe.run_check(args, report)
        assert any(item["status"] == "FAIL" for item in report.checks)
        assert password not in str(report.checks)
    finally:
        await redis.execute_command("ACL", "DELUSER", name)
        # The deny-DEL case intentionally needs administrator cleanup.
        await redis.delete(
            *(
                f"{report.redis_prefix}:{suffix}"
                for suffix in (
                    "recovery",
                    "work",
                    "events",
                    "dlq",
                    "events-dlq",
                )
            )
        )
    await verify_cleanup(engine, redis, report)


@pytest.mark.postgres
@pytest.mark.redis
async def test_parallel_checks_remain_isolated(deployed_schema: Any) -> None:
    engine, redis, args = deployed_schema
    reports = [new_report(), new_report()]
    results = await asyncio.gather(
        *(probe.run_check(args, report) for report in reports)
    )
    assert results == [True, True]
    for report in reports:
        await verify_cleanup(engine, redis, report)


@pytest.mark.postgres
@pytest.mark.redis
async def test_alembic_check_on_isolated_current_schema(
    deployed_schema: Any,
) -> None:
    from alembic import command
    from alembic.config import Config

    engine, _redis, _args = deployed_schema
    config = Config()
    config.set_main_option(
        "script_location", str(SCRIPTS.parent / "migrations")
    )
    config.attributes["configure_logger"] = False

    def check(connection: Any) -> None:
        config.attributes["connection"] = connection
        # Fixture sentinel is not application metadata; rollback restores it.
        connection.execute(text("DROP TABLE sentinel"))
        command.check(config)

    async with engine.connect() as connection:
        await connection.run_sync(check)
