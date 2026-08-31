"""Fleet-aware access to Runtime-owned storage."""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import RuntimeTargetStatus, RuntimeType
from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeFileContent,
    RuntimeFileMetadata,
    RuntimeFileRangeError,
    RuntimeFileStreamer,
    RuntimeFileUnavailableError,
)
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.runtime_drivers import (
    ConfiguredRuntimeDriverFactory,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)

logger = logging.getLogger(__name__)


class FleetRuntimeStorageAccess:
    """Read shared Runtime storage through a healthy target, preferring execution affinity."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: RuntimeTargetRegistry,
        driver_factory: ConfiguredRuntimeDriverFactory,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._driver_factory = driver_factory

    async def read_notebook(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
    ) -> dict[str, object]:
        targets = await self._candidates(runtime_type, preferred_target_id)
        if not targets:
            raise RuntimeDriverError(
                "No healthy Runtime Target can access shared storage."
            )
        last_error: Exception | None = None
        for target in targets:
            credential = self._registry.resolve_credential(
                target.credential_ref, target.credential_ciphertext
            )
            driver = self._driver_factory.create(
                target.runtime_type, target.connection_config, credential
            )
            try:
                return await driver.read_notebook(path)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Runtime storage read failed; trying another shared-storage target",
                    extra={"runtime_target_id": str(target.id)},
                )
            finally:
                await driver.close()
        raise RuntimeDriverError(
            "All Runtime Targets failed to read shared storage."
        ) from last_error

    async def write_notebook(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        notebook: dict[str, object],
    ) -> None:
        targets = await self._candidates(runtime_type, preferred_target_id)
        if not targets:
            raise RuntimeDriverError(
                "No healthy Runtime Target can access shared storage."
            )
        last_error: Exception | None = None
        for target in targets:
            credential = self._registry.resolve_credential(
                target.credential_ref, target.credential_ciphertext
            )
            driver = self._driver_factory.create(
                target.runtime_type, target.connection_config, credential
            )
            try:
                await driver.write_notebook(path, notebook)
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Runtime storage write failed; trying another shared-storage target",
                    extra={"runtime_target_id": str(target.id)},
                )
            finally:
                await driver.close()
        raise RuntimeDriverError(
            "All Runtime Targets failed to write shared storage."
        ) from last_error

    async def write_text(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        content: str,
    ) -> RuntimeFileMetadata:
        targets = await self._candidates(runtime_type, preferred_target_id)
        if not targets:
            raise RuntimeDriverError(
                "No healthy Runtime Target can access shared storage."
            )
        last_error: Exception | None = None
        for target in targets:
            credential = self._registry.resolve_credential(
                target.credential_ref, target.credential_ciphertext
            )
            driver = self._driver_factory.create(
                target.runtime_type, target.connection_config, credential
            )
            try:
                await driver.write_text(path, content)
                return await driver.file_metadata(path)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Runtime storage write failed; trying another shared-storage target",
                    extra={"runtime_target_id": str(target.id)},
                )
            finally:
                await driver.close()
        raise RuntimeDriverError(
            "All Runtime Targets failed to write shared storage."
        ) from last_error

    @asynccontextmanager
    async def open_file(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        range_header: str | None,
    ) -> AsyncIterator[RuntimeFileContent]:
        targets = await self._candidates(runtime_type, preferred_target_id)
        if not targets:
            raise RuntimeDriverError(
                "No healthy Runtime Target can access shared storage."
            )
        last_error: Exception | None = None
        for target in targets:
            credential = self._registry.resolve_credential(
                target.credential_ref, target.credential_ciphertext
            )
            driver = self._driver_factory.create(
                target.runtime_type, target.connection_config, credential
            )
            if not isinstance(driver, RuntimeFileStreamer):
                await driver.close()
                last_error = RuntimeDriverError(
                    "Runtime Driver does not support Artifact content streaming."
                )
                continue
            async with AsyncExitStack() as stack:
                stack.push_async_callback(driver.close)
                try:
                    opened = await stack.enter_async_context(
                        driver.open_file(path, range_header)
                    )
                except (RuntimeFileRangeError, RuntimeFileUnavailableError):
                    raise
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Runtime download setup failed; trying another target",
                        extra={"runtime_target_id": str(target.id)},
                    )
                    continue
                # Once metadata is handed to HTTP, never switch to another
                # target, even if zero body bytes have been emitted yet.
                yield opened
                return
        raise RuntimeDriverError(
            "All Runtime Targets failed to stream shared storage."
        ) from last_error

    async def _candidates(
        self, runtime_type: RuntimeType, preferred_target_id: UUID | None
    ) -> list[RuntimeTargetORM]:
        preferred = case(
            (RuntimeTargetORM.id == preferred_target_id, 0), else_=1
        )
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(RuntimeTargetORM)
                    .where(
                        RuntimeTargetORM.runtime_type == runtime_type,
                        RuntimeTargetORM.enabled.is_(True),
                        RuntimeTargetORM.status.in_(
                            [
                                RuntimeTargetStatus.ACTIVE,
                                RuntimeTargetStatus.DRAINING,
                            ]
                        ),
                    )
                    .order_by(preferred, RuntimeTargetORM.name)
                )
            )
