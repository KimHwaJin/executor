"""Verify that critical PostgreSQL Executor queries have usable supporting indexes."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import text

from executor_service.config import get_settings
from executor_service.infrastructure.db.session import create_engine


@dataclass(frozen=True)
class QueryPlanCheck:
    name: str
    expected_index: str
    query: str


CHECKS = (
    QueryPlanCheck(
        "execution cursor list",
        "ix_executions_created_cursor",
        "SELECT id, created_at FROM executions ORDER BY created_at DESC, id DESC LIMIT 101",
    ),
    QueryPlanCheck(
        "queued reconciliation",
        "ix_executions_status_created_at",
        "SELECT id FROM executions WHERE status = 'QUEUED' ORDER BY created_at LIMIT 100",
    ),
    QueryPlanCheck(
        "expired worker lease",
        "ix_executions_lease",
        "SELECT id FROM executions WHERE status = 'RUNNING' AND lease_expires_at < now() "
        "ORDER BY lease_expires_at LIMIT 100",
    ),
    QueryPlanCheck(
        "maximum runtime expiry",
        "ix_executions_execution_expires_at",
        "SELECT id FROM executions WHERE status = 'RUNNING' AND execution_expires_at <= now() "
        "ORDER BY status, execution_expires_at LIMIT 100",
    ),
    QueryPlanCheck(
        "retained session cleanup",
        "ix_executions_retained_session_cleanup",
        "SELECT id FROM executions WHERE status = 'FAILED' "
        "AND retry_strategy = 'FROM_FAILED_STEP' "
        "AND retained_runtime_session_until <= now() "
        "ORDER BY retained_runtime_session_until LIMIT 100",
    ),
    QueryPlanCheck(
        "pending outbox publication",
        "ix_outbox_pending",
        "SELECT id FROM outbox_events WHERE status = 'PENDING' "
        "AND available_at <= now() ORDER BY created_at LIMIT 100",
    ),
)


async def main() -> None:
    settings = get_settings()
    engine = create_engine(
        settings.database_dsn,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        async with engine.begin() as connection:
            # Small local databases naturally prefer sequential scans. Disabling them in this
            # diagnostic transaction verifies that each production query has a usable index.
            await connection.execute(text("SET LOCAL enable_seqscan = off"))
            for check in CHECKS:
                result = await connection.execute(
                    text(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {check.query}")
                )
                plan = "\n".join(str(row[0]) for row in result)
                if check.expected_index not in plan:
                    raise RuntimeError(
                        f"{check.name} did not use {check.expected_index}:\n{plan}"
                    )
                print(f"PASS {check.name}: {check.expected_index}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
