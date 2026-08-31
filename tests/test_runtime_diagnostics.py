"""Operator-visible diagnostics without credentials, SQL or user-code leaks."""

import errno
import json
import logging
from uuid import uuid4

import pytest

from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.runtime import (
    NotebookProjectionInterruptedError,
    RuntimeDriverError,
    RuntimeExecutionError,
)
from executor_service.infrastructure.diagnostic_mapping import diagnostic_for
from executor_service.infrastructure.execution_worker.failure_policy import (
    safe_error,
)
from executor_service.infrastructure.result_storage import ResultStorageError
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
    redact_message,
)


def test_safe_error_preserves_errno_without_private_filename() -> None:
    message = safe_error(
        PermissionError(errno.EACCES, "secret-value", "/private-token/file")
    )
    assert "errno=13" in message
    assert "secret-value" not in message
    assert "private-token" not in message
    assert (
        safe_error(ResultStorageError("Step result manifest checksum failed."))
        == "Step result manifest checksum failed."
    )


def test_interrupted_notebook_diagnostic_preserves_primary_retry_policy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = NotebookProjectionInterruptedError()
    detail = diagnostic_for(
        error,
        phase="NOTEBOOK_INTERRUPTED",
        category=DiagnosticCategory.NOTEBOOK,
    )
    assert detail.code == "NOTEBOOK_NOT_REFRESHED"
    assert detail.origin == "EXECUTOR"
    assert "may be stale" in detail.message
    assert "retry" not in detail.message.lower()
    log_runtime_failure(
        logging.getLogger("diagnostic-test"),
        error,
        phase="NOTEBOOK_INTERRUPTED",
    )
    assert "may be stale" in caplog.text


@pytest.mark.parametrize(
    "message",
    [
        'password="sensitive-value" token=sensitive-value',
        "redis://user:sensitive-value@localhost/3",
        "Authorization: Bearer sensitive-value",
        "Authorization: token sensitive-value",
        "api_key: 'sensitive-value'",
        '{"token":"sensitive-value"}',
    ],
)
def test_common_credentials_are_redacted(message: str) -> None:
    assert "sensitive-value" not in redact_message(message)


def test_log_preserves_context_chain_and_stack_without_sql_or_locals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution_id = uuid4()
    try:
        try:
            raise ValueError("INSERT password=sensitive-value")
        except ValueError as exc:
            raise RuntimeDriverError(
                "Jupyter REST request failed: status=500."
            ) from exc
    except RuntimeDriverError as exc:
        log_runtime_failure(
            logging.getLogger("diagnostic-test"),
            exc,
            phase="RUNTIME_PREPARE",
            execution_id=execution_id,
        )
    record = json.loads(caplog.records[-1].getMessage())
    assert record["execution_id"] == str(execution_id)
    assert record["phase"] == "RUNTIME_PREPARE"
    assert [item["type"] for item in record["errors"]] == [
        "RuntimeDriverError",
        "ValueError",
    ]
    assert record["errors"][1]["stack"][-1]["line"] > 0
    assert "status=500" in record["errors"][0]["message"]
    assert "sensitive-value" not in caplog.text
    assert "INSERT" not in caplog.text


def test_user_code_exception_message_stays_out_of_operational_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log_runtime_failure(
        logging.getLogger("diagnostic-test"),
        RuntimeExecutionError("customer-data-and-unlabelled-secret", []),
        phase="RUNTIME_EXECUTE",
    )
    assert "customer-data" not in caplog.text
    assert "inspect Step result files" in caplog.text


def test_exception_chain_is_bounded_even_when_cyclic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first
    log_runtime_failure(
        logging.getLogger("diagnostic-test"), first, phase="TEST"
    )
    assert len(json.loads(caplog.records[-1].getMessage())["errors"]) == 2
