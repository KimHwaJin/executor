"""Reject oversized input before allocation; keep DB logs value-free."""

import io
import logging
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import MaterializeArtifactCommand
from executor_service.domain.enums import ArtifactType, CodeSourceType
from executor_service.domain.errors import (
    ArtifactRegistrationError,
    InvalidExecutionSpecError,
)
from executor_service.execution_specs import ExecutionSpecResolver
from executor_service.infrastructure._materialized_artifacts.content import (
    ArtifactContentResolver,
)
from executor_service.infrastructure.db.logging import (
    DatabaseErrorFilter,
    install_database_error_filters,
)
from tests.test_execution_specs import _spec


async def test_step_limit_precedes_reading_any_source(tmp_path: Path) -> None:
    spec = _spec({"type": "PATH", "path": "missing.py", "sha256": "0" * 64})
    spec.steps.append(spec.steps[0].model_copy(update={"sequence": 1}))
    with pytest.raises(InvalidExecutionSpecError, match="maximum Step"):
        await ExecutionSpecResolver(tmp_path, max_steps=1).resolve(spec)


async def test_artifact_path_is_read_with_a_hard_byte_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.md"
    path.write_text("initial", encoding="utf-8")
    reads = []

    class GrowingFile(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    original = Path.open

    def open_file(self, *args, **kwargs):
        if self == path:
            return GrowingFile(b"x" * 1024)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    resolver = ArtifactContentResolver(tmp_path, max_bytes=8)
    command = MaterializeArtifactCommand(
        uuid4(),
        "bounded",
        ArtifactType.REPORT,
        CodeSourceType.PATH,
        None,
        "report.md",
        None,
    )
    with pytest.raises(ArtifactRegistrationError, match="size limit"):
        await resolver.resolve(command)
    assert reads == [9]


async def test_db_bind_values_are_hidden_by_engine(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE secret_test (value TEXT CHECK(length(value) < 2))"
            )
        )
    with pytest.raises(IntegrityError) as raised:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO secret_test (value) VALUES (:value)"),
                {"value": "test-only-sensitive-value"},
            )
    assert "test-only-sensitive-value" not in str(raised.value)


def test_db_filter_removes_driver_detail_and_chained_exception_text():
    class DriverError(Exception):
        sqlstate = "23505"

    try:
        try:
            raise IntegrityError(
                "INSERT sensitive-sql",
                {"token": "sensitive-param"},
                DriverError("DETAIL: sensitive-driver-value"),
            )
        except IntegrityError as exc:
            raise RuntimeError("outer sensitive-driver-value") from exc
    except RuntimeError:
        record = logging.LogRecord(
            "executor_service",
            logging.ERROR,
            __file__,
            1,
            "failed sensitive-driver-value",
            (),
            sys.exc_info(),
        )
    # A formatter/handler may have previously cached the exception text.
    record.exc_text = "cached sensitive-driver-value"
    assert DatabaseErrorFilter().filter(record)
    output = logging.Formatter("%(levelname)s %(message)s").format(record)
    # Traceback *source code* may contain test literals; the formatted error
    # object itself must have no SQL, parameters, driver DETAIL or cause.
    assert "23505" in record.getMessage()
    assert "IntegrityError" in record.getMessage()
    assert "sensitive" not in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[1] is not None
    assert "sensitive" not in str(record.exc_info[1])
    assert record.exc_info[1].__cause__ is None
    assert "Traceback" in output


def test_install_db_filters_preserves_internal_handlers_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
):
    logger = logging.getLogger("executor_service.test_internal_handler")
    # Do not add filters to pytest's capture/reporting handlers.
    monkeypatch.setattr(logging.root, "handlers", [])
    monkeypatch.setattr(
        logging.root.manager, "loggerDict", {logger.name: logger}
    )
    handler = logging.NullHandler()
    custom_filter = logging.Filter()
    formatter = logging.Formatter("INTERNAL %(message)s")
    handler.addFilter(custom_filter)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    try:
        install_database_error_filters()
        install_database_error_filters()
        assert handler.formatter is formatter
        assert custom_filter in handler.filters
        assert (
            sum(
                isinstance(item, DatabaseErrorFilter)
                for item in handler.filters
            )
            == 1
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
