"""Best-effort Runtime session cleanup shared by processors."""

import logging

from executor_service.domain.enums import RuntimeSessionCleanupStatus
from executor_service.domain.runtime import RuntimeDriver

logger = logging.getLogger(__name__)


async def best_effort_session_stop(
    driver: RuntimeDriver, runtime_session_id: str
) -> RuntimeSessionCleanupStatus:
    try:
        await driver.interrupt_session(runtime_session_id)
        await driver.delete_session(runtime_session_id)
    except Exception:
        logger.warning(
            "Runtime session cleanup failed",
            extra={"runtime_session_id": runtime_session_id},
        )
        return RuntimeSessionCleanupStatus.FAILED
    return RuntimeSessionCleanupStatus.SUCCEEDED
