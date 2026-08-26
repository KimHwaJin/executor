import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    ExecutionStatus,
    OperationMode,
    OutboxDestination,
    RuntimePool,
    RuntimeTargetStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.models import utc_now
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.infrastructure.workspace import WorkspaceManager
from executor_service.interfaces.contracts import (
    ExecutionArtifactPageResponse,
    ExecutionArtifactResponse,
)
from tests.runtime_credentials import runtime_credential_fields
from tests.runtime_storage_fake import InMemoryRuntimeStorage


def _command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="artifact-submit",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="artifact-user",
        project_id="artifact-project",
        session_id="artifact-session",
        task_id="test-task",
        steps=(
            StepSpec(
                sequence=0,
                code="print('artifact')",
                skill_name="report",
                tool_name="write_outputs",
            ),
        ),
    )


async def _seed_attempt(
    engine: AsyncEngine,
    execution_id: UUID,
    step_id: UUID,
) -> tuple[UUID, ExecutionLease]:
    target_id = uuid4()
    attempt_id = uuid4()
    now = utc_now()
    owner = "artifact-worker"
    fencing_token = 1
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        execution = await session.get(ExecutionORM, execution_id)
        if execution is None:
            raise AssertionError("Seeded Execution was not found.")
        execution.status = ExecutionStatus.RUNNING
        execution.lease_owner = owner
        execution.lease_expires_at = now + timedelta(minutes=1)
        execution.fencing_token = fencing_token
        session.add(
            RuntimeTargetORM(
                id=target_id,
                name="artifact-jupyter",
                connection_config={"endpoint": "http://127.0.0.1:8888"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
        session.add(
            ExecutionAttemptORM(
                id=attempt_id,
                execution_id=execution_id,
                attempt_number=1,
                runtime_target_id=target_id,
                runtime_session_id="artifact-kernel",
                status=AttemptStatus.RUNNING,
                lease_owner=owner,
                lease_expires_at=now + timedelta(minutes=1),
                heartbeat_at=now,
                fencing_token=fencing_token,
                started_at=now,
            )
        )
        session.add(
            ExecutionStepAttemptORM(
                execution_id=execution_id,
                execution_attempt_id=attempt_id,
                execution_step_id=step_id,
                sequence=0,
                skill_name="report",
                tool_name="write_outputs",
                input_parameters={},
                status=StepStatus.RUNNING,
                started_at=now,
            )
        )
    return target_id, ExecutionLease(
        execution_id=execution_id,
        attempt_id=attempt_id,
        owner=owner,
        fencing_token=fencing_token,
    )


async def test_artifact_discovery_manifest_lineage_and_idempotency(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_command())
    _, lease = await _seed_attempt(engine, execution.id, execution.steps[0].id)
    workspace = WorkspaceManager().plan(execution)
    manager = ExecutionArtifactManager(create_session_factory(engine))
    driver = InMemoryRuntimeStorage()
    driver.reset_storage()
    before = await manager.snapshot(driver, workspace)

    driver.put_runtime_file(
        f"{workspace.artifacts_path}/datasets/result.csv", b"value\n1\n"
    )
    driver.put_runtime_file(
        f"{workspace.artifacts_path}/plots/directory-wins.csv",
        b"not,a,dataset\n",
    )
    driver.put_runtime_file(
        f"{workspace.artifacts_path}/reports/summary.md", b"# Result\n"
    )
    processed_path = (
        "users/artifact-user/datasets/processed/asset-1/data.parquet"
    )
    driver.put_runtime_file(processed_path, b"processed-data")
    entries = [
        {
            "storage_type": "PV",
            "artifact_type": "REPORT",
            "path": "artifacts/reports/summary.md",
            "name": "workspace-relative-summary",
        },
        {
            "storage_type": "PV",
            "artifact_type": "DATASET",
            "path": processed_path,
            "name": "processed-daily-data",
            "external_parent_asset_id": "raw-daily-asset",
            "metadata": {"token": "must-not-leak", "rows": 1},
        },
        {
            "storage_type": "S3",
            "artifact_type": "MODEL",
            "uri": "s3://analysis-results/models/model.onnx",
            "name": "trained-model",
            "size_bytes": 42,
            "checksum_sha256": "a" * 64,
        },
    ]
    driver.put_runtime_file(
        workspace.manifest_path,
        "".join(json.dumps(entry) + "\n" for entry in entries).encode(),
    )

    artifact_ids = await manager.discover_and_register(
        driver=driver,
        workspace=workspace,
        before=before,
        lease=lease,
        sequence=0,
        status=ArtifactStatus.AVAILABLE,
    )
    repeated_ids = await manager.discover_and_register(
        driver=driver,
        workspace=workspace,
        before=before,
        lease=lease,
        sequence=0,
        status=ArtifactStatus.AVAILABLE,
    )

    assert len(artifact_ids) == 5
    assert repeated_ids == artifact_ids
    queries = SQLAlchemyExecutionQueryService(create_session_factory(engine))
    artifacts = await queries.artifacts(execution.id)
    assert len(artifacts) == 5
    assert {artifact.artifact_type for artifact in artifacts} == {
        ArtifactType.DATASET,
        ArtifactType.PLOT,
        ArtifactType.REPORT,
        ArtifactType.MODEL,
    }
    directory_classified = next(
        artifact
        for artifact in artifacts
        if artifact.name == "directory-wins.csv"
    )
    assert directory_classified.artifact_type == ArtifactType.PLOT
    processed_artifact = next(
        artifact
        for artifact in artifacts
        if artifact.name == "processed-daily-data"
    )
    artifact_page = ExecutionArtifactPageResponse.from_page(artifacts)
    artifact_summary = next(
        item
        for item in artifact_page.items
        if item.name == "processed-daily-data"
    )
    artifact_detail = ExecutionArtifactResponse.from_view(processed_artifact)
    assert artifact_summary.storage.size_bytes == len(b"processed-data")
    assert "uri" not in artifact_summary.storage.model_dump()
    assert artifact_detail.storage.uri.startswith("jupyter-pv://")
    assert (
        artifact_detail.lineage.external_parent_asset_id == "raw-daily-asset"
    )
    assert artifact_detail.metadata["rows"] == 1
    assert processed_artifact.storage_type == ArtifactStorageType.PV
    assert processed_artifact.external_parent_asset_id == "raw-daily-asset"
    assert processed_artifact.metadata == {
        "token": "[REDACTED]",
        "rows": 1,
        "verification": "runtime-computed",
    }
    assert processed_artifact.checksum_sha256 is not None
    relative_artifact = next(
        artifact
        for artifact in artifacts
        if artifact.name == "workspace-relative-summary"
    )
    assert (
        relative_artifact.relative_path
        == f"{workspace.artifacts_path}/reports/summary.md"
    )
    async with create_session_factory(engine)() as session:
        artifact_rows = await session.scalar(
            select(func.count(ExecutionArtifactORM.id))
        )
        artifact_events = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.destination == OutboxDestination.EVENTS
            )
        )
    assert artifact_rows == 5
    assert artifact_events == 0


async def test_manifest_rejects_path_outside_pv(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_command())
    _, lease = await _seed_attempt(engine, execution.id, execution.steps[0].id)
    workspace = WorkspaceManager().plan(execution)
    manager = ExecutionArtifactManager(create_session_factory(engine))
    driver = InMemoryRuntimeStorage()
    driver.reset_storage()
    before = await manager.snapshot(driver, workspace)
    driver.put_runtime_file(
        workspace.manifest_path,
        (
            json.dumps(
                {
                    "storage_type": "PV",
                    "artifact_type": "DATASET",
                    "path": "../outside.txt",
                }
            )
            + "\n"
        ).encode(),
    )

    with pytest.raises(
        ArtifactRegistrationError, match="Invalid Artifact manifest"
    ):
        await manager.discover_and_register(
            driver=driver,
            workspace=workspace,
            before=before,
            lease=lease,
            sequence=0,
            status=ArtifactStatus.AVAILABLE,
        )


async def test_stale_lease_cannot_register_artifact(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_command())
    _, stale_lease = await _seed_attempt(
        engine, execution.id, execution.steps[0].id
    )
    workspace = WorkspaceManager().plan(execution)
    manager = ExecutionArtifactManager(create_session_factory(engine))
    driver = InMemoryRuntimeStorage()
    driver.reset_storage()
    before = await manager.snapshot(driver, workspace)
    driver.put_runtime_file(
        f"{workspace.artifacts_path}/reports/stale.md",
        b"# stale\n",
    )
    async with create_session_factory(engine)() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(fencing_token=stale_lease.fencing_token + 1)
        )

    with pytest.raises(ExecutionLeaseLostError):
        await manager.discover_and_register(
            driver=driver,
            workspace=workspace,
            before=before,
            lease=stale_lease,
            sequence=0,
            status=ArtifactStatus.AVAILABLE,
        )

    async with create_session_factory(engine)() as session:
        artifact_count = await session.scalar(
            select(func.count(ExecutionArtifactORM.id))
        )
    assert artifact_count == 0
