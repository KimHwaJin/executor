"""Transactional commands for managing Runtime Targets."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.runtime_targets import (
    DisableRuntimeTargetCommand,
    PurgeRuntimeTargetCommand,
    RuntimeTargetPurgeView,
    RuntimeTargetView,
    SetRuntimeTargetStateCommand,
    UpsertRuntimeTargetCommand,
)
from executor_service.domain.enums import RuntimeTargetStatus
from executor_service.domain.errors import (
    IdempotencyConflictError,
    RuntimeTargetConfigurationError,
    RuntimeTargetPurgeConflictError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure._runtime_registry.credentials import (
    RuntimeCredentialCipher,
)
from executor_service.infrastructure._runtime_registry.idempotency import (
    RuntimeCommandReceipts,
    fingerprint,
    secret_hash,
)
from executor_service.infrastructure._runtime_registry.mappers import (
    purge_view,
)
from executor_service.infrastructure._runtime_registry.normalization import (
    normalize_connection_config,
)
from executor_service.infrastructure._runtime_registry.probe import (
    RuntimeTargetProber,
)
from executor_service.infrastructure._runtime_registry.queries import (
    RuntimeTargetQueries,
)
from executor_service.infrastructure._runtime_registry.targets import (
    required_target,
)
from executor_service.infrastructure.db.models import (
    CommandReceiptORM,
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
    RuntimeTargetPurgeORM,
)
from executor_service.settings import Settings


class RuntimeTargetCommands:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        credentials: RuntimeCredentialCipher,
        receipts: RuntimeCommandReceipts,
        queries: RuntimeTargetQueries,
        prober: RuntimeTargetProber,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._credentials = credentials
        self._receipts = receipts
        self._queries = queries
        self._prober = prober

    async def upsert(
        self, command: UpsertRuntimeTargetCommand
    ) -> RuntimeTargetView:
        connection_config = normalize_connection_config(
            command.runtime_type, command.connection_config
        )
        request_fingerprint = fingerprint(
            {
                "name": command.name,
                "runtime_type": command.runtime_type.value,
                "connection_config": connection_config,
                "credential_sha256": secret_hash(command.credential),
                "pool": command.pool.value,
                "max_concurrent_executions": (
                    command.max_concurrent_executions
                ),
                "actor_type": command.actor_type.value
                if command.actor_type
                else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._receipts.repeated_result(
                session,
                command.idempotency_key,
                "runtime_target.upsert",
                request_fingerprint,
            )
            if repeated_id is not None:
                target = await required_target(session, repeated_id)
                return await self._queries.view(session, target)

            target = await session.scalar(
                select(RuntimeTargetORM)
                .where(RuntimeTargetORM.name == command.name)
                .with_for_update()
            )
            if target is None:
                if not command.credential:
                    raise RuntimeTargetConfigurationError(
                        "credential is required when registering a new "
                        "Runtime Target."
                    )
                target = RuntimeTargetORM(
                    name=command.name,
                    runtime_type=command.runtime_type,
                    connection_config=connection_config,
                    credential_ref="encrypted:database",
                    credential_ciphertext=self._credentials.encrypt(
                        command.credential
                    ),
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
                        "runtime_type is immutable for an existing Runtime "
                        "Target. Register a new target name for a different "
                        "Runtime Driver."
                    )
                target.connection_config = connection_config
                target.pool = command.pool
                target.enabled = True
                if command.max_concurrent_executions is not None:
                    target.max_concurrent_executions = (
                        command.max_concurrent_executions
                    )
                if command.credential is not None:
                    target.credential_ref = "encrypted:database"
                    target.credential_ciphertext = self._credentials.encrypt(
                        command.credential
                    )
                target.updated_at = utc_now()
                if command.actor_type is not None:
                    target.updated_by_type = command.actor_type
                    target.updated_by = command.actor_id
            self._receipts.add(
                session,
                command.idempotency_key,
                "runtime_target.upsert",
                request_fingerprint,
                target.id,
            )
            target_id = target.id
        return await self._prober.probe(
            target_id,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
        )

    async def disable(
        self, command: DisableRuntimeTargetCommand
    ) -> RuntimeTargetView:
        request_fingerprint = fingerprint(
            {
                "target_id": str(command.target_id),
                "actor_type": command.actor_type.value
                if command.actor_type
                else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._receipts.repeated_result(
                session,
                command.idempotency_key,
                "runtime_target.disable",
                request_fingerprint,
            )
            if repeated_id is not None:
                target = await required_target(session, repeated_id)
                return await self._queries.view(session, target)
            target = await required_target(
                session, command.target_id, lock=True
            )
            target.enabled = False
            target.status = RuntimeTargetStatus.OFFLINE
            target.updated_at = utc_now()
            if command.actor_type is not None:
                target.updated_by_type = command.actor_type
                target.updated_by = command.actor_id
            self._receipts.add(
                session,
                command.idempotency_key,
                "runtime_target.disable",
                request_fingerprint,
                target.id,
            )
            return await self._queries.view(session, target)

    async def set_state(
        self, command: SetRuntimeTargetStateCommand
    ) -> RuntimeTargetView:
        if command.desired_state not in {
            RuntimeTargetStatus.ACTIVE,
            RuntimeTargetStatus.DRAINING,
        }:
            raise RuntimeTargetConfigurationError(
                "desired_state must be ACTIVE or DRAINING. Use disable for "
                "durable disablement."
            )
        request_fingerprint = fingerprint(
            {
                "target_id": str(command.target_id),
                "desired_state": command.desired_state.value,
                "actor_type": command.actor_type.value
                if command.actor_type
                else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            repeated_id = await self._receipts.repeated_result(
                session,
                command.idempotency_key,
                "runtime_target.set_state",
                request_fingerprint,
            )
            if repeated_id is not None:
                target = await required_target(session, repeated_id)
                return await self._queries.view(session, target)
            target = await required_target(
                session, command.target_id, lock=True
            )
            target.enabled = True
            # Probe promotes healthy OFFLINE targets to ACTIVE. It intentionally
            # preserves DRAINING so health checks never undo operator intent.
            target.status = (
                RuntimeTargetStatus.OFFLINE
                if command.desired_state == RuntimeTargetStatus.ACTIVE
                else RuntimeTargetStatus.DRAINING
            )
            target.updated_at = utc_now()
            if command.actor_type is not None:
                target.updated_by_type = command.actor_type
                target.updated_by = command.actor_id
            self._receipts.add(
                session,
                command.idempotency_key,
                "runtime_target.set_state",
                request_fingerprint,
                target.id,
            )
            target_id = target.id
        if command.desired_state == RuntimeTargetStatus.ACTIVE:
            return await self._prober.probe(
                target_id,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
            )
        return await self._queries.get(target_id)

    async def purge(
        self, command: PurgeRuntimeTargetCommand
    ) -> RuntimeTargetPurgeView:
        request_fingerprint = fingerprint(
            {
                "target_id": str(command.target_id),
                "confirmation_name": command.confirmation_name,
                "actor_type": command.actor_type.value
                if command.actor_type
                else None,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            receipt = await session.scalar(
                select(CommandReceiptORM).where(
                    CommandReceiptORM.idempotency_key
                    == command.idempotency_key
                )
            )
            if receipt is not None:
                if (
                    receipt.command_type != "runtime_target.purge"
                    or receipt.request_fingerprint != request_fingerprint
                ):
                    raise IdempotencyConflictError(
                        "idempotency_key was already used with a different "
                        "command."
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
                return purge_view(tombstone)

            target = await required_target(
                session, command.target_id, lock=True
            )
            if command.confirmation_name != target.name:
                raise RuntimeTargetPurgeConflictError(
                    "confirmation_name does not match the registered target "
                    "name."
                )
            if target.enabled or target.status != RuntimeTargetStatus.OFFLINE:
                raise RuntimeTargetPurgeConflictError(
                    "A target must be disabled and OFFLINE before it can be "
                    "purged."
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
                    "A Runtime Target referenced by Execution or Attempt "
                    "history cannot be purged."
                )

            tombstone = RuntimeTargetPurgeORM(
                target_id=target.id,
                target_name=target.name,
                runtime_type=target.runtime_type,
                connection_config=target.connection_config,
                pool=target.pool,
                idempotency_key=command.idempotency_key,
                request_fingerprint=request_fingerprint,
                created_by_type=command.actor_type,
                created_by=command.actor_id,
                updated_by_type=command.actor_type,
                updated_by=command.actor_id,
            )
            session.add(tombstone)
            await session.flush()
            await session.delete(target)
            self._receipts.add(
                session,
                command.idempotency_key,
                "runtime_target.purge",
                request_fingerprint,
                command.target_id,
            )
            await session.flush()
            return purge_view(tombstone)
