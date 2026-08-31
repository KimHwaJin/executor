"""Cross-check published evidence against DB reads and sealed result files."""

import hashlib
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.execution_results import (
    ExecutionResultQueryService,
)
from executor_service.events import ExecutionStreamEnvelope
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.interfaces._contracts.events import (
    ExecutionEventPageResponse,
)
from executor_service.interfaces._contracts.results import (
    ExecutionOperationResultResponse,
    ExecutionResultResponse,
)
from executor_service.interfaces._contracts.steps import (
    ExecutionStepAttemptResponse,
    ExecutionStepResponse,
)


async def assert_result_evidence_surfaces(
    factory: async_sessionmaker[AsyncSession],
    execution_id: UUID,
    root: Path,
) -> None:
    queries = SQLAlchemyExecutionQueryService(factory)
    results = ExecutionResultQueryService(queries)
    bundle = ExecutionResultResponse.from_bundle(
        await results.execution(execution_id)
    )
    for operation in bundle.operations:
        detail = ExecutionOperationResultResponse.from_bundle(
            await results.operation(execution_id, operation.operation_id)
        )
        assert detail.operation == operation
        for step in operation.steps:
            step_detail = ExecutionStepResponse.from_domain(
                await queries.step(execution_id, step.step_id), execution_id
            )
            assert step.result == step_detail.result

    events = ExecutionEventPageResponse.from_page(
        await queries.events(execution_id)
    )
    for event in events.items:
        envelope = ExecutionStreamEnvelope.model_validate(
            event.model_dump(
                mode="json",
                include=set(ExecutionStreamEnvelope.model_fields),
            )
        )
        assert envelope.payload == event.payload
        if event.event_type == "execution.step_completed":
            items = [{**event.payload, "step_id": event.payload["step"]["id"]}]
        elif event.event_type == "execution.operation_completed":
            items = event.payload["step_results"]
        else:
            continue
        for item in items:
            histories = await queries.attempt_steps(
                execution_id, UUID(item["attempt"]["id"])
            )
            history = next(
                row
                for row in histories.items
                if str(row.execution_step_id) == item["step_id"]
            )
            detail = ExecutionStepAttemptResponse.from_view(history)
            ref = item["result_ref"]
            assert item["status"] == detail.result.status
            if ref is None:
                assert detail.result.result_ref is None
                if event.event_type == "execution.step_completed":
                    assert item["output_summary"] is None
                continue
            assert detail.result.result_ref is not None
            assert ref == {
                **detail.result.result_ref.model_dump(
                    mode="json",
                    include={
                        "storage",
                        "relative_path",
                        "size_bytes",
                        "checksum_sha256",
                        "complete",
                    },
                ),
                "media_type": "application/json",
            }
            path = root / ref["relative_path"]
            assert not any(part.endswith(".partial") for part in path.parts)
            body = path.read_bytes()
            assert len(body) == ref["size_bytes"]
            assert hashlib.sha256(body).hexdigest() == ref["checksum_sha256"]
            manifest = json.loads(body)
            assert manifest["complete"] is ref["complete"]
            assert manifest["identity"]["execution_attempt_id"] == str(
                history.execution_attempt_id
            )
            assert manifest["identity"]["step_id"] == item["step_id"]
            if event.event_type == "execution.step_completed":
                assert item["output_summary"]["count"] == len(
                    manifest["outputs"]
                )
                assert item["output_summary"]["content_types"] == sorted(
                    {
                        representation["media_type"]
                        for output in manifest["outputs"]
                        for representation in output["representations"]
                    }
                )
