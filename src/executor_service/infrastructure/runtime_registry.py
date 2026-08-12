"""Persistent Runtime Target registry, encrypted credentials, and health monitoring."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import UTC
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.pagination import Page, decode_time_cursor, encode_time_cursor
from executor_service.application.runtime_targets import (
    PurgeRuntimeTargetCommand,
    RemoveRuntimeTargetCommand,
    RuntimePoolView,
    RuntimeTargetPurgeView,
    RuntimeTargetView,
    SetRuntimeTargetStateCommand,
    UpsertRuntimeTargetCommand,
)
from executor_service.config import Settings
from executor_service.domain.enums import (
    ActorType,
    AttemptStatus,
    ExecutionStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.domain.errors import (
    IdempotencyConflictError,
    RuntimeTargetConfigurationError,
    RuntimeTargetNotFoundError,
    RuntimeTargetPurgeConflictError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    CommandReceiptORM,
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
    RuntimeTargetPurgeORM,
)
from executor_service.infrastructure.runtime_drivers import ConfiguredRuntimeDriverFactory

logger = logging.getLogger(__name__)


class RuntimeTargetRegistry:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._driver_factory = ConfiguredRuntimeDriverFactory(settings)
        try:
            self._fernet = Fernet(settings.runtime_credential_encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeTargetConfigurationError(
                "RUNTIME_CREDENTIAL_KEY must be a valid Fernet key."
            ) from exc
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def ensure_configured_target(self, supported_profiles: list[str]) -> None:
        """Seed the environment-configured target without overwriting a manual disable."""
        if not self._settings.runtime_enabled:
            return
        pool = RuntimePool(self._settings.runtime_pool)
        async with self._session_factory() as session, session.begin():
            target = await session.scalar(
                select(RuntimeTargetORM).where(
                    RuntimeTargetORM.name == self._settings.runtime_target_name
                )
            )
            if target is None:
                session.add(
                    RuntimeTargetORM(
                        name=self._settings.runtime_target_name,
                        runtime_type=RuntimeType.JUPYTER,
                        connection_config={"endpoint": self._settings.jupyter_endpoint.rstrip("/")},
                        credential_ref="settings:JUPYTER_TOKEN",
                        credential_ciphertext=None,
                        pool=pool,
                        status=RuntimeTargetStatus.ACTIVE,
                        enabled=True,
                        max_concurrent_executions=(
                            self._settings.runtime_default_max_concurrent_executions
                        ),
                        supported_profiles=supported_profiles,
                        last_health_check_at=utc_now(),
                        last_health_error=None,
                    )
                )
                return
            if target.runtime_type != RuntimeType.JUPYTER:
                raise RuntimeTargetConfigurationError(
                    "The environment-configured Runtime Target name is already registered "
                    "with a different runtime_type. Use a distinct RUNTIME_TARGET_NAME."
                )
            target.connection_config = {"endpoint": self._settings.jupyter_endpoint.rstrip("/")}
            target.credential_ref = "settings:JUPYTER_TOKEN"
            target.credential_ciphertext = None
            target.pool = pool
            if target.enabled and target.status != RuntimeTargetStatus.DRAINING:
                target.status = RuntimeTargetStatus.ACTIVE
            target.max_concurrent_executions = (
                self._settings.runtime_default_max_concurrent_executions
            )
            target.supported_profiles = supported_profiles
            target.updated_at = utc_now()

    async def start(self) -> None:
        if self._monitor_task is not None:
            return
        self._stop_event.clear()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="runtime-fleet-health-monitor"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None

    async def upsert(self, command: UpsertRuntimeTargetCommand) -> RuntimeTargetView:
        connection_config = _normalize_connection_config(
            command.runtime_type, command.connection_config
        )
        fingerprint = _fingerprint(
            {
                "name": command.name,
                "runtime_type": command.runtime_type.value,
                "connection_config": connection_config,
                "credential_sha256": _secret_hash(command.credential),
                "pool": command.pool.value,
                "max_concurrent_executions": command.max_concurrent_executions,
                "actor_type": command.actor_type.value if command.actor_type else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._repeated_result(
                session, command.idempotency_key, "runtime_target.upsert", fingerprint
            )
            if repeated_id is not None:
                target = await self._required_target(session, repeated_id)
                return await self._to_view(session, target)

            target = await session.scalar(
                select(RuntimeTargetORM)
                .where(RuntimeTargetORM.name == command.name)
                .with_for_update()
            )
            if target is None:
                if not command.credential:
                    raise RuntimeTargetConfigurationError(
                        "credential is required when registering a new Runtime Target."
                    )
                target = RuntimeTargetORM(
                    name=command.name,
                    runtime_type=command.runtime_type,
                    connection_config=connection_config,
                    credential_ref="encrypted:database",
                    credential_ciphertext=self._encrypt(command.credential),
                    pool=command.pool,
                    status=RuntimeTargetStatus.OFFLINE,
                    enabled=True,
                    max_concurrent_executions=(
                        command.max_concurrent_executions
                        or self._settings.runtime_default_max_concurrent_executions
                    ),
                    supported_profiles=[],
                    created_by_type=command.actor_type,
                    created_by=command.actor_id,
                    updated_by_type=command.actor_type,
                    updated_by=command.actor_id,
                )
                session.add(target)
                await session.flush()
            else:
                if target.runtime_type != command.runtime_type:
                    raise RuntimeTargetConfigurationError(
                        "runtime_type is immutable for an existing Runtime Target. "
                        "Register a new target name for a different Runtime Driver."
                    )
                target.connection_config = connection_config
                target.pool = command.pool
                target.enabled = True
                if command.max_concurrent_executions is not None:
                    target.max_concurrent_executions = command.max_concurrent_executions
                if command.credential is not None:
                    target.credential_ref = "encrypted:database"
                    target.credential_ciphertext = self._encrypt(command.credential)
                target.updated_at = utc_now()
                if command.actor_type is not None:
                    target.updated_by_type = command.actor_type
                    target.updated_by = command.actor_id
            self._add_receipt(
                session,
                command.idempotency_key,
                "runtime_target.upsert",
                fingerprint,
                target.id,
            )
            target_id = target.id
        return await self.probe(target_id, actor_type=command.actor_type, actor_id=command.actor_id)

    async def list(
        self,
        pool: RuntimePool | None = None,
        *,
        status: RuntimeTargetStatus | None = None,
        enabled: bool | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[RuntimeTargetView]:
        async with self._session_factory() as session:
            statement = select(RuntimeTargetORM)
            if pool is not None:
                statement = statement.where(RuntimeTargetORM.pool == pool)
            if status is not None:
                statement = statement.where(RuntimeTargetORM.status == status)
            if enabled is not None:
                statement = statement.where(RuntimeTargetORM.enabled.is_(enabled))
            if cursor is not None:
                created_at, item_id = decode_time_cursor(cursor, "runtime_targets")
                statement = statement.where(
                    or_(
                        RuntimeTargetORM.created_at > created_at,
                        and_(
                            RuntimeTargetORM.created_at == created_at,
                            RuntimeTargetORM.id > item_id,
                        ),
                    )
                )
            statement = statement.order_by(RuntimeTargetORM.created_at, RuntimeTargetORM.id).limit(
                limit + 1
            )
            targets = list(await session.scalars(statement))
            page_targets = targets[:limit]
            views = [await self._to_view(session, target) for target in page_targets]
        next_cursor = (
            encode_time_cursor("runtime_targets", page_targets[-1].created_at, page_targets[-1].id)
            if len(targets) > limit and page_targets
            else None
        )
        return Page(items=views, next_cursor=next_cursor)

    async def pool_summaries(self) -> Sequence[RuntimePoolView]:
        async with self._session_factory() as session:
            targets = list(
                await session.scalars(
                    select(RuntimeTargetORM).order_by(RuntimeTargetORM.created_at)
                )
            )
            views = [await self._to_view(session, target) for target in targets]

        summaries: list[RuntimePoolView] = []
        for pool in RuntimePool:
            pool_views = [view for view in views if view.pool == pool]
            enabled_views = [view for view in pool_views if view.enabled]
            active_views = [
                view for view in enabled_views if view.status == RuntimeTargetStatus.ACTIVE
            ]
            health_checks = [
                view.last_health_check_at
                for view in pool_views
                if view.last_health_check_at is not None
            ]
            summaries.append(
                RuntimePoolView(
                    pool=pool,
                    target_count=len(pool_views),
                    enabled_target_count=len(enabled_views),
                    active_target_count=len(active_views),
                    draining_target_count=sum(
                        view.status == RuntimeTargetStatus.DRAINING for view in pool_views
                    ),
                    offline_target_count=sum(
                        view.status == RuntimeTargetStatus.OFFLINE for view in pool_views
                    ),
                    configured_capacity=sum(
                        view.max_concurrent_executions for view in enabled_views
                    ),
                    schedulable_capacity=sum(
                        view.max_concurrent_executions for view in active_views
                    ),
                    active_execution_count=sum(view.active_execution_count for view in pool_views),
                    available_capacity=sum(view.available_capacity for view in active_views),
                    last_health_check_at=max(health_checks) if health_checks else None,
                )
            )
        return summaries

    async def get(self, target_id: UUID) -> RuntimeTargetView:
        async with self._session_factory() as session:
            target = await self._required_target(session, target_id)
            return await self._to_view(session, target)

    async def probe(
        self,
        target_id: UUID,
        *,
        actor_type: ActorType | None = None,
        actor_id: str | None = None,
    ) -> RuntimeTargetView:
        async with self._session_factory() as session:
            target = await self._required_target(session, target_id)
            credential = self.resolve_credential(
                target.credential_ref, target.credential_ciphertext
            )
            enabled = target.enabled

        if not enabled:
            return await self.get(target_id)

        driver = self._driver_factory.create(
            target.runtime_type, target.connection_config, credential
        )
        profiles: list[str] = []
        active_session_count: int | None = None
        error: str | None = None
        try:
            status = await driver.status()
            profiles = await driver.supported_profiles()
            raw_session_count = status.get("active_session_count")
            if isinstance(raw_session_count, int):
                active_session_count = raw_session_count
        except Exception as exc:
            error = f"Probe failed ({type(exc).__name__})"
        finally:
            await driver.close()

        async with self._session_factory() as session, session.begin():
            target = await self._required_target(session, target_id, lock=True)
            if target.enabled:
                if error is None:
                    if target.status != RuntimeTargetStatus.DRAINING:
                        target.status = RuntimeTargetStatus.ACTIVE
                    target.supported_profiles = profiles
                else:
                    target.status = RuntimeTargetStatus.OFFLINE
                target.last_health_check_at = utc_now()
                target.last_health_error = error
                target.active_session_count = active_session_count
                target.updated_at = utc_now()
                if actor_type is not None:
                    target.updated_by_type = actor_type
                    target.updated_by = actor_id
            return await self._to_view(session, target)

    async def remove(self, command: RemoveRuntimeTargetCommand) -> RuntimeTargetView:
        fingerprint = _fingerprint(
            {
                "target_id": str(command.target_id),
                "actor_type": command.actor_type.value if command.actor_type else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._repeated_result(
                session, command.idempotency_key, "runtime_target.remove", fingerprint
            )
            if repeated_id is not None:
                target = await self._required_target(session, repeated_id)
                return await self._to_view(session, target)
            target = await self._required_target(session, command.target_id, lock=True)
            target.enabled = False
            target.status = RuntimeTargetStatus.OFFLINE
            target.updated_at = utc_now()
            if command.actor_type is not None:
                target.updated_by_type = command.actor_type
                target.updated_by = command.actor_id
            self._add_receipt(
                session,
                command.idempotency_key,
                "runtime_target.remove",
                fingerprint,
                target.id,
            )
            return await self._to_view(session, target)

    async def set_state(self, command: SetRuntimeTargetStateCommand) -> RuntimeTargetView:
        if command.desired_state not in {
            RuntimeTargetStatus.ACTIVE,
            RuntimeTargetStatus.DRAINING,
        }:
            raise RuntimeTargetConfigurationError(
                "desired_state must be ACTIVE or DRAINING. Use remove to disable a target."
            )
        fingerprint = _fingerprint(
            {
                "target_id": str(command.target_id),
                "desired_state": command.desired_state.value,
                "actor_type": command.actor_type.value if command.actor_type else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._repeated_result(
                session,
                command.idempotency_key,
                "runtime_target.set_state",
                fingerprint,
            )
            if repeated_id is not None:
                target = await self._required_target(session, repeated_id)
                return await self._to_view(session, target)
            target = await self._required_target(session, command.target_id, lock=True)
            target.enabled = True
            # OFFLINE lets probe promote a healthy target to ACTIVE. A DRAINING probe
            # intentionally preserves DRAINING so health checks never undo operator intent.
            target.status = (
                RuntimeTargetStatus.OFFLINE
                if command.desired_state == RuntimeTargetStatus.ACTIVE
                else RuntimeTargetStatus.DRAINING
            )
            target.updated_at = utc_now()
            if command.actor_type is not None:
                target.updated_by_type = command.actor_type
                target.updated_by = command.actor_id
            self._add_receipt(
                session,
                command.idempotency_key,
                "runtime_target.set_state",
                fingerprint,
                target.id,
            )
            target_id = target.id
        if command.desired_state == RuntimeTargetStatus.ACTIVE:
            return await self.probe(
                target_id,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
            )
        return await self.get(target_id)

    async def purge(self, command: PurgeRuntimeTargetCommand) -> RuntimeTargetPurgeView:
        fingerprint = _fingerprint(
            {
                "target_id": str(command.target_id),
                "confirmation_name": command.confirmation_name,
                "actor_type": command.actor_type.value if command.actor_type else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            receipt = await session.scalar(
                select(CommandReceiptORM).where(
                    CommandReceiptORM.idempotency_key == command.idempotency_key
                )
            )
            if receipt is not None:
                if (
                    receipt.command_type != "runtime_target.purge"
                    or receipt.request_fingerprint != fingerprint
                ):
                    raise IdempotencyConflictError(
                        "idempotency_key was already used with a different command."
                    )
                tombstone = await session.scalar(
                    select(RuntimeTargetPurgeORM).where(
                        RuntimeTargetPurgeORM.target_id == command.target_id
                    )
                )
                if tombstone is None:
                    raise RuntimeTargetPurgeConflictError(
                        "The purge receipt exists without its audit tombstone."
                    )
                return self._purge_view(tombstone)

            target = await self._required_target(session, command.target_id, lock=True)
            if command.confirmation_name != target.name:
                raise RuntimeTargetPurgeConflictError(
                    "confirmation_name does not match the registered target name."
                )
            if target.enabled or target.status != RuntimeTargetStatus.OFFLINE:
                raise RuntimeTargetPurgeConflictError(
                    "A target must be soft-deleted and OFFLINE before it can be purged."
                )
            if self._settings.runtime_enabled and target.name == self._settings.runtime_target_name:
                raise RuntimeTargetPurgeConflictError(
                    "The environment-configured default Runtime Target cannot be purged."
                )

            execution_count = await session.scalar(
                select(func.count(ExecutionORM.id)).where(
                    ExecutionORM.runtime_target_id == target.id
                )
            )
            attempt_count = await session.scalar(
                select(func.count(ExecutionAttemptORM.id)).where(
                    ExecutionAttemptORM.runtime_target_id == target.id
                )
            )
            if execution_count or attempt_count:
                raise RuntimeTargetPurgeConflictError(
                    "A Runtime Target referenced by Execution or Attempt history cannot be purged."
                )

            tombstone = RuntimeTargetPurgeORM(
                target_id=target.id,
                target_name=target.name,
                runtime_type=target.runtime_type,
                connection_config=target.connection_config,
                pool=target.pool,
                idempotency_key=command.idempotency_key,
                request_fingerprint=fingerprint,
                created_by_type=command.actor_type,
                created_by=command.actor_id,
                updated_by_type=command.actor_type,
                updated_by=command.actor_id,
            )
            session.add(tombstone)
            await session.flush()
            await session.delete(target)
            self._add_receipt(
                session,
                command.idempotency_key,
                "runtime_target.purge",
                fingerprint,
                command.target_id,
            )
            await session.flush()
            return self._purge_view(tombstone)

    async def any_active(self) -> bool:
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(RuntimeTargetORM.id)).where(
                    RuntimeTargetORM.enabled.is_(True),
                    RuntimeTargetORM.status == RuntimeTargetStatus.ACTIVE,
                )
            )
            return bool(count)

    def resolve_credential(self, credential_ref: str, credential_ciphertext: str | None) -> str:
        if credential_ref == "settings:JUPYTER_TOKEN":
            return self._settings.jupyter_auth_token
        if credential_ref == "encrypted:database" and credential_ciphertext:
            try:
                return self._fernet.decrypt(credential_ciphertext.encode("ascii")).decode()
            except (InvalidToken, UnicodeDecodeError) as exc:
                raise RuntimeTargetConfigurationError(
                    "Stored Runtime Target credential cannot be decrypted."
                ) from exc
        raise RuntimeTargetConfigurationError("Unsupported Runtime Target credential reference.")

    def _encrypt(self, credential: str) -> str:
        return self._fernet.encrypt(credential.encode()).decode("ascii")

    @staticmethod
    def _purge_view(tombstone: RuntimeTargetPurgeORM) -> RuntimeTargetPurgeView:
        purged_at = tombstone.created_at
        if purged_at.tzinfo is None:
            purged_at = purged_at.replace(tzinfo=UTC)
        return RuntimeTargetPurgeView(
            target_id=tombstone.target_id,
            name=tombstone.target_name,
            runtime_type=tombstone.runtime_type,
            connection_config=tombstone.connection_config,
            pool=tombstone.pool,
            purged_by_type=tombstone.created_by_type,
            purged_by=tombstone.created_by,
            purged_at=purged_at,
        )

    async def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with self._session_factory() as session:
                    target_ids = list(
                        await session.scalars(
                            select(RuntimeTargetORM.id).where(RuntimeTargetORM.enabled.is_(True))
                        )
                    )
                for target_id in target_ids:
                    try:
                        await self.probe(target_id)
                    except Exception:
                        logger.exception(
                            "Runtime Target health update failed",
                            extra={"target_id": target_id},
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime fleet health monitor failed")
            await asyncio.sleep(self._settings.runtime_health_poll_interval_seconds)

    async def _required_target(
        self, session: AsyncSession, target_id: UUID, *, lock: bool = False
    ) -> RuntimeTargetORM:
        statement = select(RuntimeTargetORM).where(RuntimeTargetORM.id == target_id)
        if lock:
            statement = statement.with_for_update()
        target = await session.scalar(statement)
        if target is None:
            raise RuntimeTargetNotFoundError(f"Runtime Target {target_id} was not found.")
        return target

    async def _to_view(self, session: AsyncSession, target: RuntimeTargetORM) -> RuntimeTargetView:
        active = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.runtime_target_id == target.id,
                ExecutionAttemptORM.status.in_([AttemptStatus.RUNNING, AttemptStatus.WAITING]),
            )
        )
        retained = await session.scalar(
            select(func.count(ExecutionORM.id)).where(
                ExecutionORM.runtime_target_id == target.id,
                ExecutionORM.status.in_([ExecutionStatus.FAILED, ExecutionStatus.QUEUED]),
                ExecutionORM.retryable.is_(True),
                ExecutionORM.retry_strategy == RetryStrategy.FROM_FAILED_STEP,
                ExecutionORM.retained_runtime_session_until > utc_now(),
            )
        )
        return RuntimeTargetView(
            id=target.id,
            name=target.name,
            runtime_type=target.runtime_type,
            connection_config=target.connection_config,
            pool=target.pool,
            status=target.status,
            enabled=target.enabled,
            max_concurrent_executions=target.max_concurrent_executions,
            supported_profiles=tuple(target.supported_profiles),
            active_execution_count=(active or 0) + (retained or 0),
            active_session_count=target.active_session_count,
            last_health_check_at=target.last_health_check_at,
            last_health_error=target.last_health_error,
            created_by_type=target.created_by_type,
            created_by=target.created_by,
            updated_by_type=target.updated_by_type,
            updated_by=target.updated_by,
            created_at=target.created_at,
            updated_at=target.updated_at,
        )

    async def _repeated_result(
        self,
        session: AsyncSession,
        idempotency_key: str,
        command_type: str,
        fingerprint: str,
    ) -> UUID | None:
        receipt = await session.scalar(
            select(CommandReceiptORM).where(CommandReceiptORM.idempotency_key == idempotency_key)
        )
        if receipt is None:
            return None
        if receipt.command_type != command_type or receipt.request_fingerprint != fingerprint:
            raise IdempotencyConflictError(
                "idempotency_key was already used with a different command."
            )
        return UUID(receipt.result["target_id"])

    @staticmethod
    def _add_receipt(
        session: AsyncSession,
        idempotency_key: str,
        command_type: str,
        fingerprint: str,
        target_id: UUID,
    ) -> None:
        session.add(
            CommandReceiptORM(
                idempotency_key=idempotency_key,
                command_type=command_type,
                request_fingerprint=fingerprint,
                result={"target_id": str(target_id)},
            )
        )


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalize_connection_config(
    runtime_type: RuntimeType, connection_config: dict[str, Any]
) -> dict[str, Any]:
    """Validate driver-owned connection data before it reaches persistent storage."""
    if runtime_type == RuntimeType.JUPYTER:
        endpoint = connection_config.get("endpoint")
        if (
            set(connection_config) != {"endpoint"}
            or not isinstance(endpoint, str)
            or not endpoint.startswith(("http://", "https://"))
        ):
            raise RuntimeTargetConfigurationError(
                "JUPYTER connection_config must contain only an http(s) endpoint."
            )
        return {"endpoint": endpoint.rstrip("/")}
    raise RuntimeTargetConfigurationError(f"Unsupported runtime_type: {runtime_type.value}")


def _secret_hash(secret: str | None) -> str | None:
    if secret is None:
        return None
    return hashlib.sha256(secret.encode()).hexdigest()
