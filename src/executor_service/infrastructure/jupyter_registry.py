"""Persistent Jupyter fleet registry, encrypted credentials, and health monitoring."""

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

from executor_service.application.jupyter_servers import (
    JupyterPoolView,
    JupyterServerPurgeView,
    JupyterServerView,
    PurgeJupyterServerCommand,
    RemoveJupyterServerCommand,
    SetJupyterServerStateCommand,
    UpsertJupyterServerCommand,
)
from executor_service.application.pagination import Page, decode_time_cursor, encode_time_cursor
from executor_service.config import Settings
from executor_service.domain.enums import (
    ActorType,
    AttemptStatus,
    ExecutionStatus,
    JupyterPool,
    JupyterServerStatus,
    RetryStrategy,
)
from executor_service.domain.errors import (
    IdempotencyConflictError,
    JupyterServerConfigurationError,
    JupyterServerNotFoundError,
    JupyterServerPurgeConflictError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    CommandReceiptORM,
    ExecutionAttemptORM,
    ExecutionORM,
    JupyterServerORM,
    JupyterServerPurgeORM,
)
from executor_service.infrastructure.jupyter import JupyterGateway

logger = logging.getLogger(__name__)


class JupyterServerRegistry:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        try:
            self._fernet = Fernet(settings.jupyter_credential_encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise JupyterServerConfigurationError(
                "JUPYTER_CREDENTIAL_KEY must be a valid Fernet key."
            ) from exc
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def ensure_configured_server(self, supported_kernels: list[str]) -> None:
        """Seed the environment-configured server without overwriting a manual disable."""
        if not self._settings.jupyter_enabled:
            return
        pool = JupyterPool(self._settings.jupyter_pool)
        async with self._session_factory() as session, session.begin():
            server = await session.scalar(
                select(JupyterServerORM).where(
                    JupyterServerORM.name == self._settings.jupyter_server_name
                )
            )
            if server is None:
                session.add(
                    JupyterServerORM(
                        name=self._settings.jupyter_server_name,
                        endpoint=self._settings.jupyter_endpoint.rstrip("/"),
                        credential_ref="settings:JUPYTER_TOKEN",
                        credential_ciphertext=None,
                        pool=pool,
                        status=JupyterServerStatus.ACTIVE,
                        enabled=True,
                        max_concurrent_executions=(
                            self._settings.jupyter_max_concurrent_executions
                        ),
                        supported_kernels=supported_kernels,
                        last_health_check_at=utc_now(),
                        last_health_error=None,
                    )
                )
                return
            server.endpoint = self._settings.jupyter_endpoint.rstrip("/")
            server.credential_ref = "settings:JUPYTER_TOKEN"
            server.credential_ciphertext = None
            server.pool = pool
            if server.enabled and server.status != JupyterServerStatus.DRAINING:
                server.status = JupyterServerStatus.ACTIVE
            server.max_concurrent_executions = self._settings.jupyter_max_concurrent_executions
            server.supported_kernels = supported_kernels
            server.updated_at = utc_now()

    async def start(self) -> None:
        if self._monitor_task is not None:
            return
        self._stop_event.clear()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="jupyter-fleet-health-monitor"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None

    async def upsert(self, command: UpsertJupyterServerCommand) -> JupyterServerView:
        endpoint = command.endpoint.rstrip("/")
        fingerprint = _fingerprint(
            {
                "name": command.name,
                "endpoint": endpoint,
                "token_sha256": _secret_hash(command.token),
                "pool": command.pool.value,
                "max_concurrent_executions": command.max_concurrent_executions,
                "actor_type": command.actor_type.value if command.actor_type else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._repeated_result(
                session, command.idempotency_key, "jupyter_server.upsert", fingerprint
            )
            if repeated_id is not None:
                server = await self._required_server(session, repeated_id)
                return await self._to_view(session, server)

            server = await session.scalar(
                select(JupyterServerORM)
                .where(JupyterServerORM.name == command.name)
                .with_for_update()
            )
            if server is None:
                if not command.token:
                    raise JupyterServerConfigurationError(
                        "token is required when registering a new Jupyter server."
                    )
                server = JupyterServerORM(
                    name=command.name,
                    endpoint=endpoint,
                    credential_ref="encrypted:database",
                    credential_ciphertext=self._encrypt(command.token),
                    pool=command.pool,
                    status=JupyterServerStatus.OFFLINE,
                    enabled=True,
                    max_concurrent_executions=(
                        command.max_concurrent_executions
                        or self._settings.jupyter_max_concurrent_executions
                    ),
                    supported_kernels=[],
                    created_by_type=command.actor_type,
                    created_by=command.actor_id,
                    updated_by_type=command.actor_type,
                    updated_by=command.actor_id,
                )
                session.add(server)
                await session.flush()
            else:
                server.endpoint = endpoint
                server.pool = command.pool
                server.enabled = True
                if command.max_concurrent_executions is not None:
                    server.max_concurrent_executions = command.max_concurrent_executions
                if command.token is not None:
                    server.credential_ref = "encrypted:database"
                    server.credential_ciphertext = self._encrypt(command.token)
                server.updated_at = utc_now()
                if command.actor_type is not None:
                    server.updated_by_type = command.actor_type
                    server.updated_by = command.actor_id
            self._add_receipt(
                session,
                command.idempotency_key,
                "jupyter_server.upsert",
                fingerprint,
                server.id,
            )
            server_id = server.id
        return await self.probe(server_id, actor_type=command.actor_type, actor_id=command.actor_id)

    async def list(
        self,
        pool: JupyterPool | None = None,
        *,
        status: JupyterServerStatus | None = None,
        enabled: bool | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[JupyterServerView]:
        async with self._session_factory() as session:
            statement = select(JupyterServerORM)
            if pool is not None:
                statement = statement.where(JupyterServerORM.pool == pool)
            if status is not None:
                statement = statement.where(JupyterServerORM.status == status)
            if enabled is not None:
                statement = statement.where(JupyterServerORM.enabled.is_(enabled))
            if cursor is not None:
                created_at, item_id = decode_time_cursor(cursor, "jupyter_servers")
                statement = statement.where(
                    or_(
                        JupyterServerORM.created_at > created_at,
                        and_(
                            JupyterServerORM.created_at == created_at,
                            JupyterServerORM.id > item_id,
                        ),
                    )
                )
            statement = statement.order_by(JupyterServerORM.created_at, JupyterServerORM.id).limit(
                limit + 1
            )
            servers = list(await session.scalars(statement))
            page_servers = servers[:limit]
            views = [await self._to_view(session, server) for server in page_servers]
        next_cursor = (
            encode_time_cursor("jupyter_servers", page_servers[-1].created_at, page_servers[-1].id)
            if len(servers) > limit and page_servers
            else None
        )
        return Page(items=views, next_cursor=next_cursor)

    async def pool_summaries(self) -> Sequence[JupyterPoolView]:
        async with self._session_factory() as session:
            servers = list(
                await session.scalars(
                    select(JupyterServerORM).order_by(JupyterServerORM.created_at)
                )
            )
            views = [await self._to_view(session, server) for server in servers]

        summaries: list[JupyterPoolView] = []
        for pool in JupyterPool:
            pool_views = [view for view in views if view.pool == pool]
            enabled_views = [view for view in pool_views if view.enabled]
            active_views = [
                view
                for view in enabled_views
                if view.status == JupyterServerStatus.ACTIVE
            ]
            health_checks = [
                view.last_health_check_at
                for view in pool_views
                if view.last_health_check_at is not None
            ]
            summaries.append(
                JupyterPoolView(
                    pool=pool,
                    server_count=len(pool_views),
                    enabled_server_count=len(enabled_views),
                    active_server_count=len(active_views),
                    draining_server_count=sum(
                        view.status == JupyterServerStatus.DRAINING
                        for view in pool_views
                    ),
                    offline_server_count=sum(
                        view.status == JupyterServerStatus.OFFLINE
                        for view in pool_views
                    ),
                    configured_capacity=sum(
                        view.max_concurrent_executions for view in enabled_views
                    ),
                    schedulable_capacity=sum(
                        view.max_concurrent_executions for view in active_views
                    ),
                    active_execution_count=sum(
                        view.active_execution_count for view in pool_views
                    ),
                    available_capacity=sum(view.available_capacity for view in active_views),
                    last_health_check_at=max(health_checks) if health_checks else None,
                )
            )
        return summaries

    async def get(self, server_id: UUID) -> JupyterServerView:
        async with self._session_factory() as session:
            server = await self._required_server(session, server_id)
            return await self._to_view(session, server)

    async def probe(
        self,
        server_id: UUID,
        *,
        actor_type: ActorType | None = None,
        actor_id: str | None = None,
    ) -> JupyterServerView:
        async with self._session_factory() as session:
            server = await self._required_server(session, server_id)
            endpoint = server.endpoint
            token = self.resolve_token(server.credential_ref, server.credential_ciphertext)
            enabled = server.enabled

        if not enabled:
            return await self.get(server_id)

        gateway = JupyterGateway(endpoint, token, self._settings.jupyter_request_timeout_seconds)
        kernels: list[str] = []
        active_kernel_count: int | None = None
        error: str | None = None
        try:
            status = await gateway.status()
            kernels = await gateway.kernel_specs()
            raw_kernel_count = status.get("kernels")
            if isinstance(raw_kernel_count, int):
                active_kernel_count = raw_kernel_count
        except Exception as exc:
            error = f"Probe failed ({type(exc).__name__})"
        finally:
            await gateway.close()

        async with self._session_factory() as session, session.begin():
            server = await self._required_server(session, server_id, lock=True)
            if server.enabled:
                if error is None:
                    if server.status != JupyterServerStatus.DRAINING:
                        server.status = JupyterServerStatus.ACTIVE
                    server.supported_kernels = kernels
                else:
                    server.status = JupyterServerStatus.OFFLINE
                server.last_health_check_at = utc_now()
                server.last_health_error = error
                server.active_kernel_count = active_kernel_count
                server.updated_at = utc_now()
                if actor_type is not None:
                    server.updated_by_type = actor_type
                    server.updated_by = actor_id
            return await self._to_view(session, server)

    async def remove(self, command: RemoveJupyterServerCommand) -> JupyterServerView:
        fingerprint = _fingerprint(
            {
                "server_id": str(command.server_id),
                "actor_type": command.actor_type.value if command.actor_type else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._repeated_result(
                session, command.idempotency_key, "jupyter_server.remove", fingerprint
            )
            if repeated_id is not None:
                server = await self._required_server(session, repeated_id)
                return await self._to_view(session, server)
            server = await self._required_server(session, command.server_id, lock=True)
            server.enabled = False
            server.status = JupyterServerStatus.OFFLINE
            server.updated_at = utc_now()
            if command.actor_type is not None:
                server.updated_by_type = command.actor_type
                server.updated_by = command.actor_id
            self._add_receipt(
                session,
                command.idempotency_key,
                "jupyter_server.remove",
                fingerprint,
                server.id,
            )
            return await self._to_view(session, server)

    async def set_state(self, command: SetJupyterServerStateCommand) -> JupyterServerView:
        if command.desired_state not in {
            JupyterServerStatus.ACTIVE,
            JupyterServerStatus.DRAINING,
        }:
            raise JupyterServerConfigurationError(
                "desired_state must be ACTIVE or DRAINING. Use remove to disable a server."
            )
        fingerprint = _fingerprint(
            {
                "server_id": str(command.server_id),
                "desired_state": command.desired_state.value,
                "actor_type": command.actor_type.value if command.actor_type else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._repeated_result(
                session,
                command.idempotency_key,
                "jupyter_server.set_state",
                fingerprint,
            )
            if repeated_id is not None:
                server = await self._required_server(session, repeated_id)
                return await self._to_view(session, server)
            server = await self._required_server(session, command.server_id, lock=True)
            server.enabled = True
            # OFFLINE lets probe promote a healthy server to ACTIVE. A DRAINING probe
            # intentionally preserves DRAINING so health checks never undo operator intent.
            server.status = (
                JupyterServerStatus.OFFLINE
                if command.desired_state == JupyterServerStatus.ACTIVE
                else JupyterServerStatus.DRAINING
            )
            server.updated_at = utc_now()
            if command.actor_type is not None:
                server.updated_by_type = command.actor_type
                server.updated_by = command.actor_id
            self._add_receipt(
                session,
                command.idempotency_key,
                "jupyter_server.set_state",
                fingerprint,
                server.id,
            )
            server_id = server.id
        if command.desired_state == JupyterServerStatus.ACTIVE:
            return await self.probe(
                server_id,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
            )
        return await self.get(server_id)

    async def purge(self, command: PurgeJupyterServerCommand) -> JupyterServerPurgeView:
        fingerprint = _fingerprint(
            {
                "server_id": str(command.server_id),
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
                    receipt.command_type != "jupyter_server.purge"
                    or receipt.request_fingerprint != fingerprint
                ):
                    raise IdempotencyConflictError(
                        "idempotency_key was already used with a different command."
                    )
                tombstone = await session.scalar(
                    select(JupyterServerPurgeORM).where(
                        JupyterServerPurgeORM.server_id == command.server_id
                    )
                )
                if tombstone is None:
                    raise JupyterServerPurgeConflictError(
                        "The purge receipt exists without its audit tombstone."
                    )
                return self._purge_view(tombstone)

            server = await self._required_server(session, command.server_id, lock=True)
            if command.confirmation_name != server.name:
                raise JupyterServerPurgeConflictError(
                    "confirmation_name does not match the registered server name."
                )
            if server.enabled or server.status != JupyterServerStatus.OFFLINE:
                raise JupyterServerPurgeConflictError(
                    "A server must be soft-deleted and OFFLINE before it can be purged."
                )
            if (
                self._settings.jupyter_enabled
                and server.name == self._settings.jupyter_server_name
            ):
                raise JupyterServerPurgeConflictError(
                    "The environment-configured default Jupyter server cannot be purged."
                )

            execution_count = await session.scalar(
                select(func.count(ExecutionORM.id)).where(
                    ExecutionORM.jupyter_server_id == server.id
                )
            )
            attempt_count = await session.scalar(
                select(func.count(ExecutionAttemptORM.id)).where(
                    ExecutionAttemptORM.jupyter_server_id == server.id
                )
            )
            if execution_count or attempt_count:
                raise JupyterServerPurgeConflictError(
                    "A Jupyter server referenced by Execution or Attempt history cannot be purged."
                )

            tombstone = JupyterServerPurgeORM(
                server_id=server.id,
                server_name=server.name,
                endpoint=server.endpoint,
                pool=server.pool,
                idempotency_key=command.idempotency_key,
                request_fingerprint=fingerprint,
                created_by_type=command.actor_type,
                created_by=command.actor_id,
                updated_by_type=command.actor_type,
                updated_by=command.actor_id,
            )
            session.add(tombstone)
            await session.flush()
            await session.delete(server)
            self._add_receipt(
                session,
                command.idempotency_key,
                "jupyter_server.purge",
                fingerprint,
                command.server_id,
            )
            await session.flush()
            return self._purge_view(tombstone)

    async def any_active(self) -> bool:
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(JupyterServerORM.id)).where(
                    JupyterServerORM.enabled.is_(True),
                    JupyterServerORM.status == JupyterServerStatus.ACTIVE,
                )
            )
            return bool(count)

    def resolve_token(self, credential_ref: str, credential_ciphertext: str | None) -> str:
        if credential_ref == "settings:JUPYTER_TOKEN":
            return self._settings.jupyter_auth_token
        if credential_ref == "encrypted:database" and credential_ciphertext:
            try:
                return self._fernet.decrypt(credential_ciphertext.encode("ascii")).decode()
            except (InvalidToken, UnicodeDecodeError) as exc:
                raise JupyterServerConfigurationError(
                    "Stored Jupyter credential cannot be decrypted."
                ) from exc
        raise JupyterServerConfigurationError("Unsupported Jupyter credential reference.")

    def _encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode("ascii")

    @staticmethod
    def _purge_view(tombstone: JupyterServerPurgeORM) -> JupyterServerPurgeView:
        purged_at = tombstone.created_at
        if purged_at.tzinfo is None:
            purged_at = purged_at.replace(tzinfo=UTC)
        return JupyterServerPurgeView(
            server_id=tombstone.server_id,
            name=tombstone.server_name,
            endpoint=tombstone.endpoint,
            pool=tombstone.pool,
            purged_by_type=tombstone.created_by_type,
            purged_by=tombstone.created_by,
            purged_at=purged_at,
        )

    async def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with self._session_factory() as session:
                    server_ids = list(
                        await session.scalars(
                            select(JupyterServerORM.id).where(JupyterServerORM.enabled.is_(True))
                        )
                    )
                for server_id in server_ids:
                    try:
                        await self.probe(server_id)
                    except Exception:
                        logger.exception(
                            "Jupyter server health update failed",
                            extra={"server_id": server_id},
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Jupyter fleet health monitor failed")
            await asyncio.sleep(self._settings.jupyter_health_poll_interval_seconds)

    async def _required_server(
        self, session: AsyncSession, server_id: UUID, *, lock: bool = False
    ) -> JupyterServerORM:
        statement = select(JupyterServerORM).where(JupyterServerORM.id == server_id)
        if lock:
            statement = statement.with_for_update()
        server = await session.scalar(statement)
        if server is None:
            raise JupyterServerNotFoundError(f"Jupyter server {server_id} was not found.")
        return server

    async def _to_view(self, session: AsyncSession, server: JupyterServerORM) -> JupyterServerView:
        active = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.jupyter_server_id == server.id,
                ExecutionAttemptORM.status.in_([AttemptStatus.RUNNING, AttemptStatus.WAITING]),
            )
        )
        retained = await session.scalar(
            select(func.count(ExecutionORM.id)).where(
                ExecutionORM.jupyter_server_id == server.id,
                ExecutionORM.status.in_([ExecutionStatus.FAILED, ExecutionStatus.QUEUED]),
                ExecutionORM.retryable.is_(True),
                ExecutionORM.retry_strategy == RetryStrategy.FROM_FAILED_STEP,
                ExecutionORM.retained_kernel_until > utc_now(),
            )
        )
        return JupyterServerView(
            id=server.id,
            name=server.name,
            endpoint=server.endpoint,
            pool=server.pool,
            status=server.status,
            enabled=server.enabled,
            max_concurrent_executions=server.max_concurrent_executions,
            supported_kernels=tuple(server.supported_kernels),
            active_execution_count=(active or 0) + (retained or 0),
            active_kernel_count=server.active_kernel_count,
            last_health_check_at=server.last_health_check_at,
            last_health_error=server.last_health_error,
            created_by_type=server.created_by_type,
            created_by=server.created_by,
            updated_by_type=server.updated_by_type,
            updated_by=server.updated_by,
            created_at=server.created_at,
            updated_at=server.updated_at,
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
        return UUID(receipt.result["server_id"])

    @staticmethod
    def _add_receipt(
        session: AsyncSession,
        idempotency_key: str,
        command_type: str,
        fingerprint: str,
        server_id: UUID,
    ) -> None:
        session.add(
            CommandReceiptORM(
                idempotency_key=idempotency_key,
                command_type=command_type,
                request_fingerprint=fingerprint,
                result={"server_id": str(server_id)},
            )
        )


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _secret_hash(secret: str | None) -> str | None:
    if secret is None:
        return None
    return hashlib.sha256(secret.encode()).hexdigest()
