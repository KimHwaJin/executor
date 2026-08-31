"""Runtime Driver construction and traced call helpers."""

import logging
from collections.abc import Awaitable
from uuid import UUID

from executor_service.domain.runtime import RuntimeDriver, RuntimeDriverFactory
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.tracing import TracingManager

logger = logging.getLogger(__name__)


class RuntimeDriverProvider:
    """Builds a Runtime Driver after resolving its stored credential."""

    def __init__(
        self,
        registry: RuntimeTargetRegistry,
        driver_factory: RuntimeDriverFactory,
    ) -> None:
        self._registry = registry
        self._driver_factory = driver_factory

    def create(self, target: RuntimeTargetORM) -> RuntimeDriver:
        credential = self._registry.resolve_credential(
            target.credential_ref,
            target.credential_ciphertext,
        )
        return self._driver_factory.create(
            target.runtime_type,
            target.connection_config,
            credential,
        )


async def trace_runtime[T](
    tracing: TracingManager,
    name: str,
    operation: Awaitable[T],
    *,
    execution_id: UUID,
    target_id: UUID,
    sequence: int | None = None,
) -> T:
    attributes: dict[str, object] = {
        "executor.execution.id": str(execution_id),
        "executor.runtime.target.id": str(target_id),
    }
    if sequence is not None:
        attributes["executor.step.sequence"] = sequence
    with tracing.span(name, attributes=attributes):
        try:
            return await operation
        except Exception as exc:
            log_runtime_failure(
                logger,
                exc,
                phase=name,
                execution_id=execution_id,
                target_id=target_id,
                sequence=sequence,
            )
            raise
