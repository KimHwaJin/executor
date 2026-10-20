"""Per-Execution kernel memory limits and kernel environment variables."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.domain.enums import RuntimeType
from executor_service.domain.errors import InvalidExecutionSpecError
from executor_service.infrastructure._jupyter.sessions import (
    JupyterSessionClient,
)
from executor_service.infrastructure._jupyter.transport import (
    JupyterHttpTransport,
)
from executor_service.infrastructure.db.models import ExecutionORM
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeCredentialCipher,
)
from executor_service.interfaces._contracts.execution_inputs import (
    ExecutionRuntimeInput,
)
from executor_service.settings import Settings
from tests.test_execution_service import submit_command

GIB = 1024**3
CIPHER = RuntimeCredentialCipher(Fernet.generate_key().decode())


def service(engine: AsyncEngine, tmp_path: Path, **kwargs: Any):
    session_factory = create_session_factory(engine)
    return ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        {RuntimeType.JUPYTER: ("basic",)},
        FilesystemExecutionResultStore(tmp_path),
        **kwargs,
    )


async def stored(engine: AsyncEngine, execution_id) -> ExecutionORM:
    async with create_session_factory(engine)() as session:
        return (
            await session.execute(
                select(ExecutionORM).where(ExecutionORM.id == execution_id)
            )
        ).scalar_one()


async def test_default_limit_applies_and_request_overrides(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    executions = service(
        engine,
        tmp_path,
        default_memory_limit_bytes=2 * GIB,
        max_memory_limit_bytes=8 * GIB,
    )

    default = await executions.submit(submit_command("default-limit"))
    custom = await executions.submit(
        replace(submit_command("custom-limit"), memory_limit_bytes=6 * GIB)
    )

    assert default.memory_limit_bytes == 2 * GIB
    assert custom.memory_limit_bytes == 6 * GIB
    with pytest.raises(InvalidExecutionSpecError, match="allowed maximum"):
        await executions.submit(
            replace(submit_command("too-large"), memory_limit_bytes=9 * GIB)
        )


async def test_without_settings_executions_run_unlimited(
    execution_service: ExecutionService,
) -> None:
    execution = await execution_service.submit(submit_command())

    assert execution.memory_limit_bytes is None
    assert execution.runtime_env_ciphertext is None


async def test_runtime_env_is_stored_only_encrypted(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    executions = service(engine, tmp_path, secret_cipher=CIPHER)
    env = {"S3_ACCESS_KEY": "access-value", "S3_SECRET_KEY": "secret-value"}

    plain = await executions.submit(submit_command("same-key"))
    execution = await executions.submit(
        replace(submit_command("with-env"), runtime_env=env)
    )

    row = await stored(engine, execution.id)
    assert row.runtime_env_ciphertext is not None
    assert "secret-value" not in row.runtime_env_ciphertext
    assert json.loads(CIPHER.decrypt(row.runtime_env_ciphertext)) == env
    # Secrets never enter the fingerprint, which is kept in plain text.
    assert row.request_fingerprint == plain.request_fingerprint
    assert "secret-value" not in json.dumps(row.execution_metadata)


async def test_runtime_env_requires_a_cipher(
    execution_service: ExecutionService,
) -> None:
    with pytest.raises(InvalidExecutionSpecError, match=r"runtime\.env"):
        await execution_service.submit(
            replace(submit_command(), runtime_env={"S3_SECRET_KEY": "value"})
        )


@pytest.mark.parametrize(
    "env",
    [
        {"lower_case": "value"},
        {"PATH": "/tmp"},
        {"LD_PRELOAD": "/tmp/x.so"},
        {"PYTHONPATH": "/tmp"},
        {"EXECUTOR_KERNEL_MEMORY_LIMIT_BYTES": "1"},
        {"MALLOC_ARENA_MAX": "64"},
        {"S3_SECRET_KEY": "x" * 4097},
        {"S3_SECRET_KEY": "a\x00b"},
    ],
)
def test_runtime_input_rejects_unsafe_env(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ExecutionRuntimeInput(
            type=RuntimeType.JUPYTER, profile="basic", env=env
        )


def test_runtime_input_rejects_tiny_memory_limit() -> None:
    with pytest.raises(ValidationError):
        ExecutionRuntimeInput(
            type=RuntimeType.JUPYTER,
            profile="basic",
            memory_limit_bytes=1024**2,
        )


@pytest.mark.parametrize(
    ("memory_limit_bytes", "env", "path", "extra"),
    [
        (None, {}, "/api/kernels", {}),
        (
            2 * GIB,
            {"S3_SECRET_KEY": "value"},
            "/executor/kernels",
            {"memory_limit_bytes": 2 * GIB, "env": {"S3_SECRET_KEY": "value"}},
        ),
    ],
)
async def test_plain_kernels_keep_the_stock_jupyter_endpoint(
    memory_limit_bytes: int | None,
    env: dict[str, str],
    path: str,
    extra: dict[str, Any],
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "kernel-1"})

    transport = JupyterHttpTransport("http://runtime", "token", 5, 5)
    await transport.client.aclose()
    transport.client = httpx.AsyncClient(
        base_url="http://runtime", transport=httpx.MockTransport(handle)
    )
    try:
        kernel_id = await JupyterSessionClient(transport).start(
            "basic", "workspace", memory_limit_bytes, env
        )
    finally:
        await transport.close()

    assert kernel_id == "kernel-1"
    (request,) = requests
    assert request.url.path == path
    assert json.loads(request.content) == {
        "name": "basic",
        "path": "workspace",
        **extra,
    }


def test_memory_admission_settings_require_a_default_limit() -> None:
    with pytest.raises(ValidationError, match="DEFAULT_MEMORY_LIMIT"):
        Settings(execution_user_memory_quota_bytes=8 * GIB)
    with pytest.raises(ValidationError, match="DEFAULT_MEMORY_LIMIT"):
        Settings(runtime_memory_overcommit_ratio=1.0)
    with pytest.raises(ValidationError, match="must not exceed"):
        Settings(
            execution_default_memory_limit_bytes=4 * GIB,
            execution_user_memory_quota_bytes=2 * GIB,
        )

    settings = Settings(
        execution_default_memory_limit_bytes=2 * GIB,
        execution_max_memory_limit_bytes=16 * GIB,
        execution_user_memory_quota_bytes=8 * GIB,
    )
    assert settings.max_execution_memory_limit_bytes == 8 * GIB
