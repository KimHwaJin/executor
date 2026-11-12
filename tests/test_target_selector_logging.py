from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor_service.domain.enums import RuntimePool, RuntimeType
from executor_service.infrastructure.execution_worker import target_selector
from executor_service.infrastructure.execution_worker.target_selector import (
    RuntimeTargetSelector,
)
from executor_service.settings import Settings

PREFIX = "Runtime admission"


def _target(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=name,
        name=name,
        supported_profiles=["basic"],
        max_concurrent_executions=1,
        active_session_count=None,
        session_count_observed_at=None,
        last_health_error=None,
        resource_observed_at=None,
        resource_last_error=None,
        cpu_utilization=None,
        memory_utilization=None,
    )


async def test_blocked_admission_is_logged_once_per_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reserved = {"count": 1}
    monkeypatch.setattr(
        target_selector,
        "count_runtime_reservations",
        AsyncMock(side_effect=lambda *_args: reserved["count"]),
    )
    target = _target("jupyter-a")
    listed: list[list[SimpleNamespace]] = [
        [],
        [],
        [target],
        [target],
        [target],
    ]
    session = Mock(
        scalars=AsyncMock(side_effect=lambda _query: listed.pop(0)),
        scalar=AsyncMock(return_value=target),
    )
    execution = SimpleNamespace(
        runtime_pool=RuntimePool.INTERACTIVE,
        runtime_type=RuntimeType.JUPYTER,
        runtime_profile="basic",
    )
    selector = RuntimeTargetSelector(Settings(_env_file=None, auth_clients=[]))

    with caplog.at_level("INFO"):
        assert await selector.select(session, execution) is None
        assert await selector.select(session, execution) is None
        assert await selector.select(session, execution) is None
        assert await selector.select(session, execution) is None
        reserved["count"] = 0
        assert await selector.select(session, execution) is target

    assert [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(PREFIX)
    ] == [
        "Runtime admission blocked pool=INTERACTIVE runtime_type=JUPYTER "
        "profile=basic reason=NO_ACTIVE_TARGET targets=-",
        "Runtime admission blocked pool=INTERACTIVE runtime_type=JUPYTER "
        "profile=basic reason=CAPACITY_FULL targets=jupyter-a=1/1",
        "Runtime admission resumed pool=INTERACTIVE runtime_type=JUPYTER "
        "profile=basic",
    ]
