import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIRECTORY = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))
try:
    SCRIPT = runpy.run_path(
        str(SCRIPT_DIRECTORY / "kubernetes_worker_failover_e2e.py"),
        run_name="kubernetes_worker_failover_e2e_test",
    )
finally:
    sys.path.remove(str(SCRIPT_DIRECTORY))

ValidationError = SCRIPT["ValidationError"]
ready_pod_names = SCRIPT["_ready_pod_names"]
validate_attempt_history = SCRIPT["_validate_attempt_history"]
validate_event_history = SCRIPT["_validate_event_history"]


def _pod(
    name: str, *, phase: str = "Running", ready: bool = True
) -> dict[str, Any]:
    return {
        "metadata": {"name": name},
        "status": {
            "phase": phase,
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True" if ready else "False",
                }
            ],
        },
    }


def test_ready_pod_names_returns_only_running_ready_pods() -> None:
    assert ready_pod_names(
        [
            _pod("executor-b"),
            _pod("executor-a"),
            _pod("executor-pending", phase="Pending"),
            _pod("executor-unready", ready=False),
        ]
    ) == ["executor-a", "executor-b"]


def test_event_history_requires_one_failed_and_one_success_cycle() -> None:
    validate_event_history(
        [
            {
                "event_id": "event-1",
                "event_sequence": 1,
                "event_type": "execution.started",
                "payload": {"status": "RUNNING"},
            },
            {
                "event_id": "event-2",
                "event_sequence": 2,
                "event_type": "execution.completed",
                "payload": {"status": "FAILED"},
            },
            {
                "event_id": "event-3",
                "event_sequence": 3,
                "event_type": "execution.step_started",
                "payload": {"status": "RUNNING"},
            },
            {
                "event_id": "event-4",
                "event_sequence": 4,
                "event_type": "execution.completed",
                "payload": {"status": "SUCCEEDED"},
            },
        ]
    )


@pytest.mark.parametrize(
    "events, message",
    [
        (
            [
                {
                    "event_id": "event-1",
                    "event_sequence": 2,
                    "event_type": "execution.completed",
                    "payload": {"status": "FAILED"},
                }
            ],
            "not contiguous",
        ),
        (
            [
                {
                    "event_id": "event-1",
                    "event_sequence": 1,
                    "event_type": "execution.completed",
                    "payload": {"status": "FAILED"},
                },
                {
                    "event_id": "event-1",
                    "event_sequence": 2,
                    "event_type": "execution.completed",
                    "payload": {"status": "SUCCEEDED"},
                },
            ],
            "Duplicate",
        ),
    ],
)
def test_event_history_rejects_sequence_and_identity_corruption(
    events: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_event_history(events)


def test_attempt_history_proves_fencing_cleanup_and_from_start_retry() -> None:
    validate_attempt_history(
        [
            {
                "lease": {"owner": "executor-old"},
                "state": {"status": "FAILED"},
                "failure": {"type": "LEASE_EXPIRED"},
                "recovery": {"runtime_session_cleanup_status": "SUCCEEDED"},
                "runtime": {"session_id": "kernel-old"},
            },
            {
                "lease": {"owner": "executor-new"},
                "state": {"status": "SUCCEEDED"},
                "failure": None,
                "recovery": {"runtime_session_cleanup_status": "NOT_REQUIRED"},
                "runtime": {"session_id": "kernel-new"},
            },
        ],
        deleted_owner="executor-old",
        initial_session_id="kernel-old",
    )


def test_attempt_history_rejects_reused_abandoned_session() -> None:
    attempts = [
        {
            "lease": {"owner": "executor-old"},
            "state": {"status": "FAILED"},
            "failure": {"type": "LEASE_EXPIRED"},
            "recovery": {"runtime_session_cleanup_status": "SUCCEEDED"},
            "runtime": {"session_id": "kernel-old"},
        },
        {
            "lease": {"owner": "executor-new"},
            "state": {"status": "SUCCEEDED"},
            "failure": None,
            "recovery": {"runtime_session_cleanup_status": "NOT_REQUIRED"},
            "runtime": {"session_id": "kernel-old"},
        },
    ]
    with pytest.raises(ValidationError, match="reused"):
        validate_attempt_history(
            attempts,
            deleted_owner="executor-old",
            initial_session_id="kernel-old",
        )
