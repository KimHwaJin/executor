import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    JupyterPool,
    JupyterServerStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.models import utc_now
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionStepAttemptORM,
    JupyterServerORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import SQLAlchemyExecutionQueryService
from executor_service.infrastructure.workspace import WorkspaceManager


def _command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="artifact-submit",
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        jupyter_pool=JupyterPool.INTERACTIVE,
        kernel_name="python3",
        code_source_type=CodeSourceType.INLINE,
        code="print('artifact')",
        code_path=None,
        requested_by_user_id="artifact-user",
        project_id="artifact-project",
        session_id="artifact-session",
        execution_plan_id="artifact-plan",
        steps=(StepSpec(sequence=0, skill_name="report", tool_name="write_outputs"),),
    )


async def _seed_attempt(
    engine: AsyncEngine,
    execution_id: UUID,
    step_id: UUID,
) -> tuple[UUID, UUID]:
    server_id = uuid4()
    attempt_id = uuid4()
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            JupyterServerORM(
                id=server_id,
                name="artifact-jupyter",
                endpoint="http://127.0.0.1:8888",
                credential_ref="settings:JUPYTER_TOKEN",
                pool=JupyterPool.INTERACTIVE,
                status=JupyterServerStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_kernels=["python3"],
                enabled=True,
            )
        )
        session.add(
            ExecutionAttemptORM(
                id=attempt_id,
                execution_id=execution_id,
                attempt_number=1,
                jupyter_server_id=server_id,
                kernel_id="artifact-kernel",
                status=AttemptStatus.RUNNING,
                lease_owner="artifact-worker",
                lease_expires_at=now + timedelta(minutes=1),
                heartbeat_at=now,
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
                outputs=[],
                started_at=now,
            )
        )
    return server_id, attempt_id


async def test_artifact_discovery_manifest_lineage_and_idempotency(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution = await execution_service.submit(_command())
    _, attempt_id = await _seed_attempt(engine, execution.id, execution.steps[0].id)
    settings = Settings(
        jupyter_enabled=False,
        workspace_host_root=tmp_path,
        workspace_jupyter_root="/workspace/pv",
    )
    workspace = WorkspaceManager(tmp_path).prepare(execution)
    manager = ExecutionArtifactManager(create_session_factory(engine), settings)
    before = manager.snapshot(workspace)

    (workspace.datasets_dir / "result.csv").write_text("value\n1\n", encoding="utf-8")
    (workspace.plots_dir / "directory-wins.csv").write_text(
        "not,a,dataset\n", encoding="utf-8"
    )
    (workspace.reports_dir / "summary.md").write_text("# Result\n", encoding="utf-8")
    processed = (
        tmp_path
        / "users"
        / "artifact-user"
        / "datasets"
        / "processed"
        / "asset-1"
        / "data.parquet"
    )
    processed.parent.mkdir(parents=True)
    processed.write_bytes(b"processed-data")
    manifest = workspace.artifacts_dir / "manifest.jsonl"
    entries = [
        {
            "storage_type": "PV",
            "artifact_type": "DATASET",
            "path": "/workspace/pv/users/artifact-user/datasets/processed/asset-1/data.parquet",
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
    manifest.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )

    artifact_ids = await manager.discover_and_register(
        workspace=workspace,
        before=before,
        execution_id=execution.id,
        attempt_id=attempt_id,
        sequence=0,
        status=ArtifactStatus.AVAILABLE,
    )
    repeated_ids = await manager.discover_and_register(
        workspace=workspace,
        before=before,
        execution_id=execution.id,
        attempt_id=attempt_id,
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
        artifact for artifact in artifacts if artifact.name == "directory-wins.csv"
    )
    assert directory_classified.artifact_type == ArtifactType.PLOT
    processed_artifact = next(
        artifact for artifact in artifacts if artifact.name == "processed-daily-data"
    )
    assert processed_artifact.storage_type == ArtifactStorageType.PV
    assert processed_artifact.external_parent_asset_id == "raw-daily-asset"
    assert processed_artifact.metadata == {
        "token": "[REDACTED]",
        "rows": 1,
        "verification": "executor-computed",
    }
    assert processed_artifact.checksum_sha256 is not None
    trace = await queries.trace(execution.id)
    assert trace.artifacts == tuple(artifacts)

    async with create_session_factory(engine)() as session:
        artifact_rows = await session.scalar(select(func.count(ExecutionArtifactORM.id)))
        artifact_events = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.event_type == "execution.artifact_registered"
            )
        )
    assert artifact_rows == 5
    assert artifact_events == 5


async def test_manifest_rejects_path_outside_pv(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution = await execution_service.submit(_command())
    _, attempt_id = await _seed_attempt(engine, execution.id, execution.steps[0].id)
    settings = Settings(jupyter_enabled=False, workspace_host_root=tmp_path)
    workspace = WorkspaceManager(tmp_path).prepare(execution)
    manager = ExecutionArtifactManager(create_session_factory(engine), settings)
    before = manager.snapshot(workspace)
    outside = tmp_path.parent / f"outside-{uuid4()}.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace.artifacts_dir / "manifest.jsonl").write_text(
        json.dumps(
            {
                "storage_type": "PV",
                "artifact_type": "DATASET",
                "path": str(outside),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactRegistrationError, match="Invalid Artifact manifest"):
        await manager.discover_and_register(
            workspace=workspace,
            before=before,
            execution_id=execution.id,
            attempt_id=attempt_id,
            sequence=0,
            status=ArtifactStatus.AVAILABLE,
        )

    outside.unlink()
