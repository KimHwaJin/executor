"""Validate the complete local Executor test topology before load or soak runs."""

import asyncio
import os
from typing import Any
from uuid import uuid4

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from local_test_support import (
    env_bool,
    executor_http_url,
    executor_mcp_url,
    register_local_runtime_targets,
    write_report,
)
from mcp import Client
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REQUIRED_TOOLS = {
    "execution_submit",
    "execution_get",
    "execution_result_get",
    "execution_notebook_read",
    "runtime_target_upsert",
    "runtime_target_list",
}


async def _http_checks(base_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        responses = {
            path: await client.get(path)
            for path in (
                "/healthz",
                "/readyz",
                "/workerz",
                "/api/v1/runtime-targets",
            )
        }
    failures = {
        path: {
            "status_code": response.status_code,
            "body": response.text[:1000],
        }
        for path, response in responses.items()
        if response.status_code != 200
    }
    if failures:
        raise RuntimeError(f"Executor HTTP preflight failed: {failures}")
    worker = responses["/workerz"].json()
    if not worker.get("accepting_new_executions"):
        raise RuntimeError(f"Executor Worker is not accepting work: {worker}")
    return {
        "health": responses["/healthz"].json(),
        "readiness": responses["/readyz"].json(),
        "worker": worker,
    }


async def _database_check() -> dict[str, Any]:
    database_url = os.getenv(
        "LOCAL_TEST_DATABASE_URL",
        os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://executor:executor@127.0.0.1:5432/executor",
        ),
    )
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            server_version = await connection.scalar(text("SELECT version()"))
            database_name = await connection.scalar(
                text("SELECT current_database()")
            )
            migration_version = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
    finally:
        await engine.dispose()
    expected_head = ScriptDirectory.from_config(
        Config("alembic.ini")
    ).get_current_head()
    if migration_version != expected_head:
        raise RuntimeError(
            f"Alembic revision mismatch: database={migration_version!r}, code={expected_head!r}."
        )
    return {
        "database": database_name,
        "server_version": str(server_version).split(",", maxsplit=1)[0],
        "alembic_revision": migration_version,
    }


async def _redis_check() -> dict[str, Any]:
    redis_url = os.getenv(
        "LOCAL_TEST_REDIS_URL",
        os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
    )
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        if not await redis.ping():
            raise RuntimeError("Redis PING returned false.")
        info = await redis.info(section="server")
        database_size = await redis.dbsize()
    finally:
        await redis.aclose()
    return {
        "redis_version": info.get("redis_version"),
        "database_size": database_size,
    }


async def _mcp_and_runtime_checks(run_id: str) -> dict[str, Any]:
    include_batch = env_bool("LOCAL_TEST_INCLUDE_BATCH", True)
    include_secondary = env_bool("LOCAL_TEST_INCLUDE_SECONDARY", True)
    async with Client(executor_mcp_url()) as client:
        discovered = await client.list_tools()
        tool_names = {tool.name for tool in discovered.tools}
        missing = sorted(REQUIRED_TOOLS - tool_names)
        if missing:
            raise RuntimeError(
                f"Executor MCP is missing required Tools: {missing}"
            )
        targets = await register_local_runtime_targets(
            client,
            run_id=run_id,
            include_batch=include_batch,
            include_secondary=include_secondary,
        )
    return {
        "tool_count": len(tool_names),
        "runtime_targets": [
            {
                "target_id": target["target_id"],
                "name": target["name"],
                "pool": target["runtime"]["pool"],
                "profiles": target["runtime"]["supported_profiles"],
                "capacity": target["capacity"],
                "resources": target["resources"],
            }
            for target in targets
        ],
    }


async def main() -> None:
    run_id = uuid4().hex
    # Runtime admin remains reachable while `/readyz` reports an empty or stale fleet. Restore
    # topology-specific endpoints first, then require full readiness. This ordering is essential
    # after real-process tests replace Compose-internal endpoints with host endpoints.
    database, redis, mcp = await asyncio.gather(
        _database_check(),
        _redis_check(),
        _mcp_and_runtime_checks(run_id),
    )
    http = await _http_checks(executor_http_url())
    report = write_report(
        "local-test-preflight",
        run_id,
        {
            "status": "PASSED",
            "executor_url": executor_http_url(),
            "mcp_url": executor_mcp_url(),
            "http": http,
            "database": database,
            "redis": redis,
            "mcp": mcp,
        },
    )
    print("status: PASSED")
    print("runtime_target_count:", len(mcp["runtime_targets"]))
    print("report:", report)


if __name__ == "__main__":
    asyncio.run(main())
