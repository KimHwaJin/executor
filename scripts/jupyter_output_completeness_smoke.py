"""Live Jupyter output-loss check with isolated Worker DB and result storage.

Requires JUPYTER_GATEWAY_ENDPOINT and JUPYTER_GATEWAY_TOKEN. No Docker or
running Executor is required. The installed Jupyter rate limit must reject the
selected stdout workload (5 MiB by default); server limits are never changed.
"""

import asyncio
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cryptography.fernet import Fernet
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select

from executor_service.application.commands import (
    CreateOperationCommand,
    FinalizeExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.domain.results import StepResultReference
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import (
    create_engine,
    create_session_factory,
)
from executor_service.infrastructure.diagnostic_store import (
    SQLAlchemyDiagnosticQueryService,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.interfaces._contracts.steps import ExecutionStepResponse


def workload(scenario: str, size_mib: int = 5) -> str:
    if scenario == "data_limit":
        return (
            f"import sys\nsys.stdout.write('x' * ({size_mib} * 1024 * 1024))"
            "\nsys.stdout.flush()"
        )
    if scenario == "kernel_warning":
        warning = (
            "IOPub data rate exceeded.\n"
            "The Jupyter server will temporarily stop sending output\n"
            "to the client in order to avoid crashing it.\n"
            "To change this limit, set the config variable\n"
            "`--ServerApp.iopub_data_rate_limit`.\n\n"
            "Current values:\n"
            "ServerApp.iopub_data_rate_limit=1000000.0 (bytes/sec)\n"
            "ServerApp.rate_limit_window=3.0 (secs)\n\n"
        )
        return f"import sys\nsys.stderr.write({warning!r})\nsys.stderr.flush()"
    visual = (
        "print('complete-output-control')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.figure(figsize=(3, 2))\n"
        "plt.plot([1, 2, 3], [1, 4, 9])\n"
        "plt.title('Completion check')\nplt.show()\nplt.close('all')"
    )
    if scenario == "tool_error":
        return visual + "\nraise ValueError('expected after image')"
    if scenario == "timeout":
        return visual + "\nimport time\ntime.sleep(120)"
    return visual


async def run_case(
    root: Path,
    endpoint: str,
    token: str,
    mode: OperationMode,
    scenario: str,
    size_mib: int,
) -> dict[str, object]:
    cipher_key = Fernet.generate_key()
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    factory = create_session_factory(engine)
    settings = Settings(
        _env_file=None,
        runtime_enabled=False,
        shared_storage_root=root,
        runtime_credential_key=SecretStr(cipher_key.decode()),
    )
    store = FilesystemExecutionResultStore(root)
    # The runner is called directly; this client never consumes/publishes work.
    redis = Redis.from_url("redis://127.0.0.1:6379/15")
    runtime_session_id: str | None = None
    submitted_id = None
    service = ExecutionService(
        lambda: SQLAlchemyUnitOfWork(factory),
        {RuntimeType.JUPYTER: ("default", "3102311")},
        store,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session, session.begin():
            session.add(
                RuntimeTargetORM(
                    name="live-output-validation",
                    connection_config={"endpoint": endpoint},
                    credential_ref="encrypted:database",
                    credential_ciphertext=Fernet(cipher_key)
                    .encrypt(token.encode())
                    .decode(),
                    pool=RuntimePool.INTERACTIVE,
                    status=RuntimeTargetStatus.ACTIVE,
                    max_concurrent_executions=1,
                    supported_profiles=["default", "3102311"],
                    enabled=True,
                )
            )
        execution = await service.submit(
            SubmitExecutionCommand(
                idempotency_key=str(uuid4()),
                operation_mode=mode,
                operation_wait_timeout_seconds=600
                if mode == OperationMode.MULTI
                else None,
                trigger_type=TriggerType.INTERACTIVE,
                runtime_profile=os.getenv(
                    "JUPYTER_GATEWAY_PROFILE", "default"
                ),
                user_id="diagnostics-smoke",
                project_id="output-completeness",
                session_id=str(uuid4()),
                task_id=f"{mode}-{scenario}",
                steps=(
                    StepSpec(
                        sequence=0,
                        code=workload(scenario, size_mib),
                        step_timeout_seconds=10
                        if scenario == "timeout"
                        else 30,
                    ),
                    StepSpec(
                        sequence=1,
                        code="print('followup-control')",
                        step_timeout_seconds=30,
                    ),
                ),
            )
        )
        submitted_id = execution.id
        worker = ExecutionWorker(
            session_factory=factory,
            redis=redis,
            settings=settings,
            registry=RuntimeTargetRegistry(factory, settings),
            artifact_manager=ExecutionArtifactManager(factory),
            result_store=store,
        )
        async with asyncio.timeout(90):
            await worker._runner.run(execution.id)
        result = await service.get(execution.id)
        runtime_session_id = result.runtime_session_id
        step = result.steps[0]
        detail = ExecutionStepResponse.from_domain(step, execution.id)
        ref = detail.result.result_ref
        if ref is None:
            raise AssertionError("Step result evidence is missing.")
        outputs = await store.read_step_outputs(
            StepResultReference(
                relative_path=ref.relative_path,
                checksum_sha256=ref.checksum_sha256,
                size_bytes=ref.size_bytes,
                execution_attempt_id=ref.attempt_id,
                fencing_token=ref.fencing_token,
            )
        )
        async with factory() as session:
            operation = await session.get(
                ExecutionOperationORM, execution.active_operation_id
            )
            events = list(
                await session.scalars(
                    select(OutboxEventORM).where(
                        OutboxEventORM.aggregate_id == execution.id,
                    )
                )
            )
        assert operation is not None
        if scenario == "data_limit":
            assert step.status == StepStatus.FAILED, (
                "Expected the server rate limit to suppress output. Check "
                "server policy; do not raise limits to pass this test."
            )
            assert result.failure_type == FailureType.OUTPUT_LIMIT_EXCEEDED
            assert operation.status == OperationStatus.FAILED
            assert ref.complete is False
            assert result.steps[1].status == StepStatus.SKIPPED
            assert "data rate limit" in (step.error_message or "")
            assert any(
                output.get("name") == "stderr"
                and "IOPub data rate exceeded." in str(output.get("text"))
                for output in outputs
            )
            assert result.status == (
                ExecutionStatus.FAILED
                if mode == OperationMode.SINGLE
                else ExecutionStatus.WAITING_FOR_OPERATION
            )
        elif scenario in {"tool_error", "timeout"}:
            assert step.status == StepStatus.FAILED
            assert operation.status == OperationStatus.FAILED
            assert ref.complete is (scenario == "tool_error")
            assert result.steps[1].status == StepStatus.SKIPPED
            assert result.status == (
                ExecutionStatus.FAILED
                if mode == OperationMode.SINGLE
                else ExecutionStatus.WAITING_FOR_OPERATION
            )
            if mode == OperationMode.SINGLE:
                assert result.failure_type == (
                    FailureType.TOOL_ERROR
                    if scenario == "tool_error"
                    else FailureType.STEP_TIMEOUT
                )
            assert (
                "expected after image"
                if scenario == "tool_error"
                else "Step timeout"
            ) in (step.error_message or "")
        else:
            assert step.status == StepStatus.SUCCEEDED
            assert operation.status == OperationStatus.SUCCEEDED
            assert ref.complete is True
            assert result.steps[1].status == StepStatus.SUCCEEDED
            assert result.status == (
                ExecutionStatus.SUCCEEDED
                if mode == OperationMode.SINGLE
                else ExecutionStatus.WAITING_FOR_OPERATION
            )
            expected = (
                "IOPub data rate exceeded."
                if scenario == "kernel_warning"
                else "complete-output-control"
            )
            assert any(
                expected in str(output.get("text")) for output in outputs
            )
        assert result.notebook_projection_status == "SUCCEEDED"
        diagnostics = await SQLAlchemyDiagnosticQueryService(factory).list(
            execution.id
        )
        if scenario == "data_limit":
            assert any(
                item.diagnostic.code == "OUTPUT_DATA_RATE_LIMIT_EXCEEDED"
                and item.step_id == step.id
                for item in diagnostics.items
            )
        elif scenario in {"tool_error", "timeout"}:
            assert diagnostics.items
        else:
            assert not diagnostics.items
        assert result.notebook_path is not None
        gateway = JupyterRuntimeDriver(endpoint, token)
        try:
            notebook = await gateway.read_notebook(result.notebook_path)
        finally:
            await gateway.close()
        assert len(notebook["cells"]) == 2
        assert notebook["cells"][0]["outputs"] == outputs
        step_events = [
            event
            for event in events
            if event.event_type == "execution.step_completed"
        ]
        first_step_event = next(
            event.execution_event
            for event in step_events
            if event.execution_event is not None
            and event.execution_event.payload["step"]["sequence"] == 0
        )
        assert first_step_event is not None
        assert first_step_event.payload["status"] == step.status.value
        event_ref = first_step_event.payload["result_ref"]
        assert event_ref["relative_path"] == ref.relative_path
        assert event_ref["complete"] is ref.complete
        manifest_bytes = (root / ref.relative_path).read_bytes()
        assert event_ref["size_bytes"] == len(manifest_bytes)
        assert (
            event_ref["checksum_sha256"]
            == hashlib.sha256(manifest_bytes).hexdigest()
        )
        manifest = json.loads(manifest_bytes)
        assert manifest["complete"] is event_ref["complete"]
        assert first_step_event.payload["output_summary"]["count"] == len(
            manifest["outputs"]
        )
        operation_event = next(
            item.execution_event
            for item in events
            if item.event_type == "execution.operation_completed"
        )
        assert operation_event is not None
        assert operation_event.payload["step_results"][0]["result_ref"] == (
            event_ref
        )
        if scenario == "data_limit":
            assert notebook["cells"][1]["outputs"] == []
            assert (
                first_step_event.payload["error"]["message"]
                == step.error_message
            )
            assert event_ref["complete"] is False
        assert (
            sum(
                event.event_type == "execution.operation_completed"
                for event in events
            )
            == 1
        )
        assert sum(
            event.event_type == "execution.completed" for event in events
        ) == (1 if mode == OperationMode.SINGLE else 0)
        if scenario in {"normal", "tool_error", "timeout"}:
            assert any(
                isinstance(data := output.get("data"), dict)
                and "image/png" in data
                for output in outputs
            )
        finalized = False
        if scenario == "background":
            assert mode == OperationMode.MULTI
            # Fault only this disposable DB's target, never the shared server.
            async with factory() as session, session.begin():
                target = await session.scalar(select(RuntimeTargetORM))
                assert target is not None
                original_connection = dict(target.connection_config)
                target.connection_config = {"endpoint": "http://127.0.0.1:9"}
            try:
                await worker._multi_lifecycle.audit()
                await worker._multi_lifecycle.audit()
                observed = await service.get(execution.id)
                assert observed.status == ExecutionStatus.WAITING_FOR_OPERATION
                assert observed.version == result.version
                diagnostics = await SQLAlchemyDiagnosticQueryService(
                    factory
                ).list(execution.id)
                assert len(diagnostics.items) == 1
                assert (
                    diagnostics.items[0].diagnostic.phase
                    == "MULTI_SESSION_PROBE"
                )
                assert (
                    diagnostics.items[0].diagnostic.code
                    == "RUNTIME_UNAVAILABLE"
                )
            finally:
                async with factory() as session, session.begin():
                    target = await session.scalar(select(RuntimeTargetORM))
                    assert target is not None
                    target.connection_config = original_connection
            # Natural wait-expiry path must delete the actual retained kernel.
            async with factory() as session, session.begin():
                row = await session.get(ExecutionORM, execution.id)
                assert row is not None
                row.operation_wait_expires_at = utc_now() - timedelta(
                    seconds=1
                )
            await worker._multi_lifecycle.audit()
            result = await service.get(execution.id)
            runtime_session_id = result.runtime_session_id
            assert result.status == ExecutionStatus.FAILED
            assert result.failure_type == FailureType.OPERATION_WAIT_TIMEOUT
            assert (
                result.runtime_session_cleanup_status
                == RuntimeSessionCleanupStatus.SUCCEEDED
            )
            assert runtime_session_id is None
            async with factory() as session:
                events = list(
                    await session.scalars(
                        select(OutboxEventORM).where(
                            OutboxEventORM.aggregate_id == execution.id,
                        )
                    )
                )
            assert (
                sum(
                    event.event_type == "execution.completed"
                    for event in events
                )
                == 1
            )
        if mode == OperationMode.MULTI and scenario not in {
            "data_limit",
            "background",
        }:
            # Exercise an additional Operation and terminal delivery without
            # replaying earlier code or using the running Executor database.
            await service.create_operation(
                CreateOperationCommand(
                    execution_id=execution.id,
                    idempotency_key=str(uuid4()),
                    expected_version=result.version,
                    steps=(StepSpec(2, "print('multi-completion-control')"),),
                )
            )
            async with asyncio.timeout(90):
                await worker._runner.run(execution.id)
            waiting = await service.get(execution.id)
            assert waiting.status == ExecutionStatus.WAITING_FOR_OPERATION
            await service.finalize_execution(
                FinalizeExecutionCommand(
                    execution_id=execution.id,
                    idempotency_key=str(uuid4()),
                    expected_version=waiting.version,
                )
            )
            async with asyncio.timeout(90):
                await worker._runner.run(execution.id)
            result = await service.get(execution.id)
            runtime_session_id = result.runtime_session_id
            assert result.status == ExecutionStatus.SUCCEEDED
            assert runtime_session_id is None
            assert [item.status for item in result.steps] == (
                [StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.SUCCEEDED]
                if scenario in {"tool_error", "timeout"}
                else [StepStatus.SUCCEEDED] * 3
            )
            assert result.steps[0].result_manifest_path == ref.relative_path
            assert result.steps[0].result_complete is ref.complete
            assert result.notebook_path is not None
            gateway = JupyterRuntimeDriver(endpoint, token)
            try:
                notebook = await gateway.read_notebook(result.notebook_path)
            finally:
                await gateway.close()
            assert len(notebook["cells"]) == 3
            assert notebook["cells"][0]["outputs"] == outputs
            async with factory() as session:
                events = list(
                    await session.scalars(
                        select(OutboxEventORM).where(
                            OutboxEventORM.aggregate_id == execution.id,
                        )
                    )
                )
            assert (
                sum(
                    event.event_type == "execution.operation_completed"
                    for event in events
                )
                == 2
            )
            assert (
                sum(
                    event.event_type == "execution.completed"
                    for event in events
                )
                == 1
            )
            finalized = True
        return {
            "mode": mode.value,
            "scenario": scenario,
            "status": "PASSED",
            "execution_id": str(execution.id),
            "execution_status": result.status.value,
            "step_status": step.status.value,
            "operation_status": operation.status.value,
            "complete": ref.complete,
            "multi_finalized": finalized,
            "failure": result.failure_type,
            "reason": step.error_message,
            "saved_output_bytes": ref.total_size_bytes,
            "requested_output_mib": size_mib
            if scenario == "data_limit"
            else None,
            "notebook_path": result.notebook_path,
            "outbox_event_count": len(events),
            "diagnostic_codes": [
                item.diagnostic.code for item in diagnostics.items
            ],
        }
    finally:
        # Includes failed assertions and aborted probes; never clean another
        # execution's kernel or reset the user's Executor DB/Redis.
        if submitted_id is not None and runtime_session_id is None:
            runtime_session_id = (
                await service.get(submitted_id)
            ).runtime_session_id
        try:
            if runtime_session_id is not None:
                gateway = JupyterRuntimeDriver(endpoint, token)
                try:
                    await gateway.delete_session(runtime_session_id)
                finally:
                    await gateway.close()
        finally:
            await redis.aclose()
            await engine.dispose()


async def main() -> None:
    endpoint = os.getenv("JUPYTER_GATEWAY_ENDPOINT")
    token = os.getenv("JUPYTER_GATEWAY_TOKEN")
    if not endpoint or token is None:
        raise RuntimeError(
            "Set JUPYTER_GATEWAY_ENDPOINT and JUPYTER_GATEWAY_TOKEN "
            "(empty allowed for tokenless local Jupyter)."
        )
    reports = []
    size_mib = int(os.getenv("JUPYTER_GATEWAY_OUTPUT_MIB", "5"))
    if not 1 <= size_mib <= 25:
        raise ValueError(
            "JUPYTER_GATEWAY_OUTPUT_MIB must be between 1 and 25."
        )
    with TemporaryDirectory(prefix="executor-output-check-") as temporary:
        for mode in (OperationMode.SINGLE, OperationMode.MULTI):
            scenarios = (
                "normal",
                "kernel_warning",
                "data_limit",
                "tool_error",
                "timeout",
            )
            if mode == OperationMode.MULTI:
                scenarios += ("background",)
            for scenario in scenarios:
                report = await run_case(
                    Path(temporary), endpoint, token, mode, scenario, size_mib
                )
                reports.append(report)
                print(json.dumps(report), flush=True)
    print(json.dumps({"status": "PASSED", "cases": len(reports)}))


if __name__ == "__main__":
    asyncio.run(main())
