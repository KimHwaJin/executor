"""Best-effort Runtime session cleanup shared by processors."""

import logging

from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import RuntimeSessionCleanupStatus
from executor_service.domain.runtime import RuntimeDriver
from executor_service.infrastructure.diagnostic_store import DiagnosticRecorder
from executor_service.infrastructure.execution_leases import (
    CancellationLease,
    ExecutionLease,
)
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
)

logger = logging.getLogger(__name__)


async def best_effort_session_stop(
    driver: RuntimeDriver,
    runtime_session_id: str,
    *,
    lease: ExecutionLease | CancellationLease | None = None,
    diagnostics: DiagnosticRecorder | None = None,
) -> RuntimeSessionCleanupStatus:
    phase = "RUNTIME_INTERRUPT"
    try:
        await driver.interrupt_session(runtime_session_id)
        phase = "RUNTIME_DELETE"
        await driver.delete_session(runtime_session_id)
    except Exception as exc:
        if diagnostics is not None and lease is not None:
            await diagnostics.record(
                lease, exc, phase=phase, category=DiagnosticCategory.CLEANUP
            )
        log_runtime_failure(
            logger,
            exc,
            phase=phase,
            level=logging.WARNING,
            runtime_session_id=runtime_session_id,
            execution_id=lease.execution_id if lease else None,
            attempt_id=lease.attempt_id
            if isinstance(lease, ExecutionLease)
            else None,
        )
        return RuntimeSessionCleanupStatus.FAILED
    return RuntimeSessionCleanupStatus.SUCCEEDED
