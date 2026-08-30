"""Lease-fenced persistence for discovered Execution Artifacts."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.infrastructure._artifacts.models import (
    ArtifactDescriptor,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    require_active_lease,
)


class ArtifactPersistence:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def persist(
        self,
        descriptors: list[ArtifactDescriptor],
        *,
        lease: ExecutionLease,
        sequence: int,
        allow_cancel_requested: bool = False,
    ) -> list[UUID]:
        if not descriptors:
            return []
        async with self._session_factory() as session, session.begin():
            await require_active_lease(
                session,
                lease,
                allowed_statuses=(
                    (
                        ExecutionStatus.RUNNING,
                        ExecutionStatus.CANCEL_REQUESTED,
                    )
                    if allow_cancel_requested
                    else (ExecutionStatus.RUNNING,)
                ),
            )
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == lease.execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            step_attempt = await session.scalar(
                select(ExecutionStepAttemptORM).where(
                    ExecutionStepAttemptORM.execution_attempt_id
                    == lease.attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
            )
            if step is None or step_attempt is None:
                raise ArtifactRegistrationError(
                    "Artifact cannot be linked because its Execution Step "
                    "is missing."
                )
            artifact_ids: list[UUID] = []
            for descriptor in descriptors:
                identity_hash = artifact_identity_hash(
                    lease.execution_id,
                    lease.attempt_id,
                    step_attempt.id,
                    descriptor,
                )
                existing = await session.scalar(
                    select(ExecutionArtifactORM).where(
                        ExecutionArtifactORM.identity_hash == identity_hash
                    )
                )
                if existing is not None:
                    artifact_ids.append(existing.id)
                    continue
                row = ExecutionArtifactORM(
                    execution_id=lease.execution_id,
                    execution_attempt_id=lease.attempt_id,
                    execution_step_id=step.id,
                    execution_step_attempt_id=step_attempt.id,
                    parent_artifact_id=descriptor.parent_artifact_id,
                    external_parent_asset_id=(
                        descriptor.external_parent_asset_id
                    ),
                    artifact_type=descriptor.artifact_type,
                    storage_type=descriptor.storage_type,
                    status=descriptor.status,
                    name=descriptor.name,
                    description=descriptor.description,
                    uri=descriptor.uri,
                    relative_path=descriptor.relative_path,
                    media_type=descriptor.media_type,
                    size_bytes=descriptor.size_bytes,
                    checksum_sha256=descriptor.checksum_sha256,
                    artifact_metadata=descriptor.metadata,
                    identity_hash=identity_hash,
                    created_by_type=(
                        step.updated_by_type or step.created_by_type
                    ),
                    created_by=step.updated_by or step.created_by,
                    updated_by_type=(
                        step.updated_by_type or step.created_by_type
                    ),
                    updated_by=step.updated_by or step.created_by,
                )
                session.add(row)
                await session.flush()
                artifact_ids.append(row.id)
            return artifact_ids


def artifact_identity_hash(
    execution_id: UUID,
    attempt_id: UUID,
    step_attempt_id: UUID,
    descriptor: ArtifactDescriptor,
) -> str:
    payload = {
        "execution_id": str(execution_id),
        "attempt_id": str(attempt_id),
        "step_attempt_id": str(step_attempt_id),
        "uri": descriptor.uri,
        "checksum_sha256": descriptor.checksum_sha256,
        "status": descriptor.status.value,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
