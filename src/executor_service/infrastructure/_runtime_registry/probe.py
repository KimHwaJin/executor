"""Runtime Target health and resource probing."""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.runtime_targets import RuntimeTargetView
from executor_service.config import Settings
from executor_service.domain.enums import ActorType, RuntimeTargetStatus
from executor_service.domain.errors import RuntimeTargetConfigurationError
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeResourceObservation
from executor_service.infrastructure._runtime_registry.credentials import (
    RuntimeCredentialCipher,
)
from executor_service.infrastructure._runtime_registry.mappers import (
    as_float,
    as_int,
    resource_source,
    runtime_target_view,
)
from executor_service.infrastructure._runtime_registry.targets import (
    required_target,
)
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.runtime_drivers import (
    ConfiguredRuntimeDriverFactory,
)

logger = logging.getLogger(__name__)


class RuntimeTargetProber:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        credentials: RuntimeCredentialCipher,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._credentials = credentials
        self._driver_factory = ConfiguredRuntimeDriverFactory(settings)

    async def probe(
        self,
        target_id: UUID,
        *,
        actor_type: ActorType | None = None,
        actor_id: str | None = None,
    ) -> RuntimeTargetView:
        async with self._session_factory() as session:
            target = await required_target(session, target_id)
            credential = self._credentials.resolve(
                target.credential_ref, target.credential_ciphertext
            )
            enabled = target.enabled

        if not enabled:
            return await self._view(target_id)

        driver = self._driver_factory.create(
            target.runtime_type, target.connection_config, credential
        )
        profiles: list[str] = []
        active_session_count: int | None = None
        error: str | None = None
        resource: RuntimeResourceObservation | None = None
        resource_error: str | None = None
        try:
            status = await driver.status()
            reported_profiles = await driver.supported_profiles()
            allowed_profiles = set(self._settings.runtime_allowed_profiles)
            profiles = [
                profile
                for profile in reported_profiles
                if profile in allowed_profiles
            ]
            if not profiles:
                raise RuntimeTargetConfigurationError(
                    "Runtime Target supports none of RUNTIME_ALLOWED_PROFILES."
                )
            raw_session_count = status.get("active_session_count")
            if type(raw_session_count) is not int or raw_session_count < 0:
                raise RuntimeTargetConfigurationError(
                    "Runtime Target did not return a non-negative "
                    "active_session_count."
                )
            active_session_count = raw_session_count
        except Exception as exc:
            error = f"Probe failed ({type(exc).__name__})"
        if error is None:
            try:
                resource = await driver.resource_status()
            except Exception as exc:
                resource_error = (
                    f"Resource probe failed ({type(exc).__name__})"
                )
        else:
            resource_error = (
                "Resource probe skipped because health probe failed."
            )
        await driver.close()

        async with self._session_factory() as session, session.begin():
            target = await required_target(session, target_id, lock=True)
            if target.enabled:
                checked_at = utc_now()
                if error is None:
                    if target.status != RuntimeTargetStatus.DRAINING:
                        target.status = RuntimeTargetStatus.ACTIVE
                    target.supported_profiles = profiles
                else:
                    target.status = RuntimeTargetStatus.OFFLINE
                target.last_health_check_at = checked_at
                target.last_health_error = error
                if error is None and active_session_count is not None:
                    target.active_session_count = active_session_count
                    target.session_count_observed_at = checked_at
                target.resource_last_check_at = utc_now()
                target.resource_last_error = resource_error
                if resource is not None:
                    _apply_resource_observation(target, resource)
                target.updated_at = utc_now()
                if actor_type is not None:
                    target.updated_by_type = actor_type
                    target.updated_by = actor_id
            view = await runtime_target_view(session, target, self._settings)
            if (
                view.session_count_fresh
                and view.active_session_count is not None
                and view.active_session_count > view.active_execution_count
            ):
                logger.warning(
                    "Runtime session count exceeds durable reservations",
                    extra={
                        "runtime_target_id": str(view.id),
                        "active_execution_count": (
                            view.active_execution_count
                        ),
                        "active_session_count": view.active_session_count,
                    },
                )
            return view

    async def _view(self, target_id: UUID) -> RuntimeTargetView:
        async with self._session_factory() as session:
            target = await required_target(session, target_id)
            return await runtime_target_view(
                session,
                target,
                self._settings,
            )


def _apply_resource_observation(
    target: RuntimeTargetORM,
    resource: RuntimeResourceObservation,
) -> None:
    target.resource_observed_at = resource.observed_at
    target.resource_source = resource_source(resource)
    target.resource_estimated = bool(
        resource.cpu.estimated or resource.memory.estimated
    )
    target.resource_process_count = resource.process_count
    target.cpu_used_cores = as_float(resource.cpu.used)
    target.cpu_capacity_cores = as_float(resource.cpu.capacity)
    target.cpu_utilization = resource.cpu.utilization
    target.memory_used_bytes = as_int(resource.memory.used)
    target.memory_capacity_bytes = as_int(resource.memory.capacity)
    target.memory_utilization = resource.memory.utilization
    target.resource_errors = list(resource.cpu.errors + resource.memory.errors)
