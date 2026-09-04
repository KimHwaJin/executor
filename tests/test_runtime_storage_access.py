from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.domain.enums import (
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.runtime_storage import (
    FleetRuntimeStorageAccess,
)
from executor_service.settings import Settings
from tests.runtime_credentials import runtime_credential_fields


class ReadDriver:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.closed = False

    async def read_notebook(self, path: str) -> dict[str, Any]:
        if isinstance(self.result, Exception):
            raise self.result
        return {**self.result, "path": path}

    async def close(self) -> None:
        self.closed = True


class DriverFactory:
    def __init__(self, results: dict[str, dict[str, Any] | Exception]) -> None:
        self.results = results
        self.created: list[str] = []
        self.drivers: list[ReadDriver] = []

    def create(
        self,
        runtime_type: RuntimeType,
        connection_config: dict[str, Any],
        credential: str,
    ) -> ReadDriver:
        assert runtime_type == RuntimeType.JUPYTER
        assert credential == "test-token"
        endpoint = str(connection_config["endpoint"])
        self.created.append(endpoint)
        driver = ReadDriver(self.results[endpoint])
        self.drivers.append(driver)
        return driver


async def _target(
    engine: AsyncEngine,
    *,
    name: str,
    endpoint: str,
    status: RuntimeTargetStatus = RuntimeTargetStatus.ACTIVE,
    enabled: bool = True,
) -> UUID:
    session_factory = create_session_factory(engine)
    target = RuntimeTargetORM(
        id=uuid4(),
        name=name,
        runtime_type=RuntimeType.JUPYTER,
        connection_config={"endpoint": endpoint},
        **runtime_credential_fields(),
        pool=RuntimePool.INTERACTIVE,
        status=status,
        max_concurrent_executions=2,
        supported_profiles=["basic"],
        enabled=enabled,
    )
    async with session_factory() as session, session.begin():
        session.add(target)
    return target.id


def _access(
    engine: AsyncEngine, factory: DriverFactory
) -> FleetRuntimeStorageAccess:
    session_factory = create_session_factory(engine)
    settings = Settings()
    registry = RuntimeTargetRegistry(session_factory, settings)
    return FleetRuntimeStorageAccess(
        session_factory,
        registry,
        cast(Any, factory),
    )


async def test_runtime_storage_prefers_execution_target(
    engine: AsyncEngine,
) -> None:
    preferred_id = await _target(
        engine, name="z-preferred", endpoint="http://preferred"
    )
    await _target(engine, name="a-other", endpoint="http://other")
    factory = DriverFactory(
        {"http://preferred": {"cells": []}, "http://other": {"cells": [1]}}
    )

    result = await _access(engine, factory).read_notebook(
        RuntimeType.JUPYTER, preferred_id, "shared/execution.ipynb"
    )

    assert result["cells"] == []
    assert factory.created == ["http://preferred"]
    assert all(driver.closed for driver in factory.drivers)


async def test_runtime_storage_falls_back_to_another_shared_target(
    engine: AsyncEngine,
) -> None:
    preferred_id = await _target(
        engine, name="a-preferred", endpoint="http://preferred"
    )
    await _target(engine, name="b-fallback", endpoint="http://fallback")
    factory = DriverFactory(
        {
            "http://preferred": RuntimeError("offline"),
            "http://fallback": {"cells": [1]},
        }
    )

    result = await _access(engine, factory).read_notebook(
        RuntimeType.JUPYTER, preferred_id, "shared/execution.ipynb"
    )

    assert result["cells"] == [1]
    assert factory.created == ["http://preferred", "http://fallback"]
    assert all(driver.closed for driver in factory.drivers)


async def test_runtime_storage_excludes_offline_and_disabled_targets(
    engine: AsyncEngine,
) -> None:
    await _target(
        engine,
        name="offline",
        endpoint="http://offline",
        status=RuntimeTargetStatus.OFFLINE,
    )
    await _target(
        engine, name="disabled", endpoint="http://disabled", enabled=False
    )
    factory = DriverFactory({})

    with pytest.raises(RuntimeDriverError, match="No healthy Runtime Target"):
        await _access(engine, factory).read_notebook(
            RuntimeType.JUPYTER, None, "shared/execution.ipynb"
        )


async def test_runtime_storage_reports_all_target_failures(
    engine: AsyncEngine,
) -> None:
    await _target(engine, name="first", endpoint="http://first")
    await _target(engine, name="second", endpoint="http://second")
    factory = DriverFactory(
        {
            "http://first": RuntimeError("one"),
            "http://second": RuntimeError("two"),
        }
    )

    with pytest.raises(RuntimeDriverError, match="All Runtime Targets failed"):
        await _access(engine, factory).read_notebook(
            RuntimeType.JUPYTER, None, "shared/execution.ipynb"
        )
