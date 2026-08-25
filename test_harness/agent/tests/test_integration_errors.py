"""Tests for safe asynchronous integration diagnostics."""

from executor_test_agent.integrations.errors import exception_summary


def test_exception_summary_unwraps_groups_and_redacts_url_credentials() -> None:
    error = ExceptionGroup(
        "transport",
        [
            RuntimeError("outer"),
            ExceptionGroup(
                "nested",
                [
                    FileNotFoundError("missing manifest.json"),
                    ConnectionError("redis://secret@localhost:6379 unavailable"),
                ],
            ),
        ],
    )

    summary = exception_summary(error)

    assert "RuntimeError: outer" in summary
    assert "FileNotFoundError: missing manifest.json" in summary
    assert "redis://***@localhost:6379 unavailable" in summary
    assert "secret" not in summary
