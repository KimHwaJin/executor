from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from executor_service.application.services import ExecutionService
from executor_service.domain.enums import RuntimeType
from executor_service.domain.errors import (
    RuntimeTargetConfigurationError,
    UnsupportedRuntimeCapabilityError,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeOffering
from executor_service.infrastructure.db.models import ExecutionORM
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_worker import lease_recovery
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_adapters import (
    ADAPTER_TYPES,
    JupyterRuntimeAdapter,
    RuntimeAdapterRegistry,
    artifact_uri,
    normalize_connection_config,
)
from executor_service.settings import Settings
from tests.test_execution_service import multi_submit_command, submit_command

ONE_SHOT = replace(JupyterRuntimeAdapter.capabilities, stateful_sessions=False)


def _service(engine: AsyncEngine, tmp_path: Path) -> ExecutionService:
    session_factory = create_session_factory(engine)
    return ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        {RuntimeType.JUPYTER: RuntimeOffering(("basic",), ONE_SHOT)},
        FilesystemExecutionResultStore(tmp_path),
    )


def test_every_runtime_type_has_an_adapter() -> None:
    assert set(ADAPTER_TYPES) == set(RuntimeType)


def test_registry_offers_configured_profiles_and_capabilities() -> None:
    settings = Settings(_env_file=None, runtime_allowed_profiles=("basic",))
    offering = RuntimeAdapterRegistry(settings).offerings()[
        RuntimeType.JUPYTER
    ]

    assert offering.profiles == ("basic",)
    assert offering.capabilities == JupyterRuntimeAdapter.capabilities


def test_connection_config_is_validated_by_the_adapter() -> None:
    assert normalize_connection_config(
        RuntimeType.JUPYTER, {"endpoint": "http://jupyter:8888/"}
    ) == {"endpoint": "http://jupyter:8888"}
    with pytest.raises(RuntimeTargetConfigurationError):
        normalize_connection_config(
            RuntimeType.JUPYTER, {"endpoint": "ftp://jupyter"}
        )


def test_artifact_uri_uses_the_adapter_scheme() -> None:
    assert (
        artifact_uri(RuntimeType.JUPYTER, "users/u/report.md")
        == "jupyter-pv:///users/u/report.md"
    )


async def test_multi_needs_a_runtime_with_stateful_sessions(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    service = _service(engine, tmp_path)

    with pytest.raises(UnsupportedRuntimeCapabilityError):
        await service.submit(multi_submit_command("one-shot-multi"))
    assert (await service.submit(submit_command("one-shot-single"))).id


async def test_runtime_without_journal_is_not_handed_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lease_recovery,
        "runtime_capabilities",
        lambda _runtime_type: replace(
            JupyterRuntimeAdapter.capabilities, execution_journal=False
        ),
    )
    execution = SimpleNamespace(
        runtime_type=RuntimeType.JUPYTER, runtime_session_id="kernel"
    )

    # The capability check comes first, so the session is never queried.
    assert not await lease_recovery.LeaseRecoveryProcessor._hand_over(
        cast(AsyncSession, None), cast(ExecutionORM, execution), utc_now()
    )
