"""Runtime Driver construction and traced call helpers."""

import json
import logging
from collections.abc import Awaitable
from uuid import UUID

from executor_service.domain.models import Execution
from executor_service.domain.runtime import RuntimeDriver, RuntimeDriverFactory
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)

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

    async def start_session(
        self,
        driver: RuntimeDriver,
        execution: Execution,
        working_directory: str,
    ) -> str:
        """Start the Execution's kernel with its memory limit and env."""

        env = (
            json.loads(
                self._registry.decrypt(execution.runtime_env_ciphertext)
            )
            if execution.runtime_env_ciphertext
            else {}
        )
        return await driver.start_session(
            execution.runtime_profile,
            working_directory,
            execution.memory_limit_bytes,
            env,
        )


async def run_runtime_operation[T](
    name: str,
    operation: Awaitable[T],
    *,
    execution_id: UUID,
    target_id: UUID,
    sequence: int | None = None,
) -> T:
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
