from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.domain.enums import (
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.domain.runtime import (
    RuntimeByteRange,
    RuntimeDriverError,
    RuntimeFileContent,
    RuntimeFileMetadata,
)
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

    async def write_notebook(
        self, path: str, notebook: dict[str, Any]
    ) -> None:
        await self.read_notebook(path)

    async def write_text(self, path: str, content: str) -> None:
        await self.read_notebook(path)

    async def file_metadata(self, path: str) -> RuntimeFileMetadata:
        return RuntimeFileMetadata(
            path=path,
            name="file",
            size_bytes=2,
            modified_ns=0,
            media_type="text/plain",
            checksum_sha256="a" * 64,
        )

    @asynccontextmanager
    async def open_file(
        self, path: str, range_header: str | None
    ) -> AsyncIterator[RuntimeFileContent]:
        await self.read_notebook(path)

        async def body() -> AsyncIterator[bytes]:
            yield b"ok"

        yield RuntimeFileContent(
            RuntimeByteRange(0, 1, 2, False), "a" * 64, body()
        )


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
    pool: RuntimePool = RuntimePool.INTERACTIVE,
) -> UUID:
    session_factory = create_session_factory(engine)
    target = RuntimeTargetORM(
        id=uuid4(),
        name=name,
        runtime_type=RuntimeType.JUPYTER,
        connection_config={"endpoint": endpoint},
        **runtime_credential_fields(),
        pool=pool,
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


async def _storage_operation(
    access: FleetRuntimeStorageAccess,
    operation: str,
    pool: RuntimePool,
    preferred_id: UUID | None,
) -> None:
    # Deliberately identical paths across pools must never bypass isolation.
    path = "same/path/file"
    if operation == "read":
        await access.read_notebook(
            RuntimeType.JUPYTER, preferred_id, path, runtime_pool=pool
        )
    elif operation == "notebook_write":
        await access.write_notebook(
            RuntimeType.JUPYTER, preferred_id, path, {}, runtime_pool=pool
        )
    elif operation == "text_write":
        await access.write_text(
            RuntimeType.JUPYTER, preferred_id, path, "ok", runtime_pool=pool
        )
    else:
        async with access.open_file(
            RuntimeType.JUPYTER, preferred_id, path, None, runtime_pool=pool
        ) as opened:
            assert b"".join([part async for part in opened.body]) == b"ok"


@pytest.mark.parametrize("pool", list(RuntimePool))
@pytest.mark.parametrize(
    "operation", ["read", "notebook_write", "text_write", "download"]
)
@pytest.mark.parametrize(
    "preferred", ["failing", "missing", "other_pool", "none", "offline"]
)
async def test_all_storage_operations_stay_in_execution_pool(
    engine: AsyncEngine, pool: RuntimePool, operation: str, preferred: str
) -> None:
    other_pool = next(item for item in RuntimePool if item != pool)
    other_id = await _target(
        engine, name="a-other", endpoint="http://other", pool=other_pool
    )
    preferred_id = await _target(
        engine,
        name="z-preferred",
        endpoint="http://preferred",
        pool=pool,
        status=(
            RuntimeTargetStatus.OFFLINE
            if preferred == "offline"
            else RuntimeTargetStatus.ACTIVE
        ),
        enabled=preferred in {"failing", "offline"},
    )
    await _target(
        engine,
        name="b-fallback",
        endpoint="http://fallback",
        pool=pool,
        status=RuntimeTargetStatus.DRAINING,
    )
    factory = DriverFactory(
        {
            "http://preferred": RuntimeDriverError("offline"),
            "http://fallback": {"cells": []},
        }
    )
    selected = {
        "failing": preferred_id,
        "offline": preferred_id,
        "missing": uuid4(),
        "other_pool": other_id,
        "none": None,
    }[preferred]
    await _storage_operation(
        _access(engine, factory), operation, pool, selected
    )
    assert factory.created == (
        ["http://preferred", "http://fallback"]
        if preferred == "failing"
        else ["http://fallback"]
    )
    assert all(driver.closed for driver in factory.drivers)


@pytest.mark.parametrize("pool", list(RuntimePool))
@pytest.mark.parametrize(
    "operation", ["read", "notebook_write", "text_write", "download"]
)
@pytest.mark.parametrize("same_pool_fails", [False, True])
async def test_other_pool_is_never_used_as_last_resort(
    engine: AsyncEngine,
    pool: RuntimePool,
    operation: str,
    same_pool_fails: bool,
) -> None:
    other_pool = next(item for item in RuntimePool if item != pool)
    other_id = await _target(
        engine, name="a-other", endpoint="http://other", pool=other_pool
    )
    factory = DriverFactory({"http://same": RuntimeDriverError("offline")})
    if same_pool_fails:
        await _target(engine, name="same", endpoint="http://same", pool=pool)
    with pytest.raises(RuntimeDriverError):
        await _storage_operation(
            _access(engine, factory), operation, pool, other_id
        )
    assert factory.created == (["http://same"] if same_pool_fails else [])
    assert all(driver.closed for driver in factory.drivers)


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
        RuntimeType.JUPYTER,
        preferred_id,
        "shared/execution.ipynb",
        runtime_pool=RuntimePool.INTERACTIVE,
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
        RuntimeType.JUPYTER,
        preferred_id,
        "shared/execution.ipynb",
        runtime_pool=RuntimePool.INTERACTIVE,
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
            RuntimeType.JUPYTER,
            None,
            "shared/execution.ipynb",
            runtime_pool=RuntimePool.INTERACTIVE,
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
            RuntimeType.JUPYTER,
            None,
            "shared/execution.ipynb",
            runtime_pool=RuntimePool.INTERACTIVE,
        )
