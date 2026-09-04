"""Startup migrations and a DB-wide transaction lock shared with the CLI."""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.settings import Settings

logger = logging.getLogger(__name__)
# Stable, database-local lock shared by all Executor releases and CLI runs.
MIGRATION_LOCK_ID = 0x455845434D494752
# Alembic's environment proxy is process-global, even with async connections.
_alembic_lock = asyncio.Lock()


class DatabaseMigrationError(RuntimeError):
    """Safe startup failure without SQL parameters or connection secrets."""


@contextmanager
def migration_transaction(
    connection: Connection, settings: Settings
) -> Iterator[None]:
    if connection.dialect.name != "postgresql":
        raise DatabaseMigrationError("Executor migrations require PostgreSQL.")
    transaction = (
        nullcontext() if connection.in_transaction() else connection.begin()
    )
    with transaction:
        for name, seconds in (
            ("lock_timeout", settings.db_migration_lock_timeout_seconds),
            (
                "statement_timeout",
                settings.db_migration_statement_timeout_seconds,
            ),
        ):
            connection.execute(
                text("SELECT set_config(:name, :value, true)"),
                {"name": name, "value": f"{seconds * 1000}ms"},
            )
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": MIGRATION_LOCK_ID},
        )
        yield
    # PostgreSQL releases the transaction lock on commit, rollback, or disconnect.


async def upgrade_database(engine: AsyncEngine, settings: Settings) -> None:
    """Upgrade before any Worker/admission initialization; fail startup closed."""
    acquired = False
    logger.info("Database migration starting")
    try:
        async with asyncio.timeout(settings.db_migration_lock_timeout_seconds):
            await _alembic_lock.acquire()
        acquired = True
        config = Config()
        migration_path = await asyncio.to_thread(
            settings.db_migrations_path.resolve
        )
        config.set_main_option(
            "script_location", str(migration_path).replace("%", "%%")
        )
        config.attributes["configure_logger"] = False
        config.attributes["migration_settings"] = settings
        async with engine.begin() as connection:

            def upgrade(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(upgrade)
    except Exception as exc:
        # Driver errors may contain passwords/SQL. Log only type and SQLSTATE.
        original = getattr(exc, "orig", exc)
        sqlstate = getattr(original, "sqlstate", None)
        code = str(sqlstate) if sqlstate else "unknown"
        logger.error(
            "Database migration failed; error_type=%s sqlstate=%s",
            type(exc).__name__,
            code,
        )
        raise DatabaseMigrationError(
            "Database migration failed; startup stopped "
            f"(error_type={type(exc).__name__}, sqlstate={code}). "
            "Check database availability, DDL permissions, migration files "
            "and migration lock/statement timeouts."
        ) from None
    finally:
        if acquired:
            _alembic_lock.release()
    logger.info("Database migration completed")
