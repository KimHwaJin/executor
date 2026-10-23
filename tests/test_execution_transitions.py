"""The Execution status transition table."""

import pytest

from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import InvalidStateTransitionError
from executor_service.domain.transitions import (
    EXECUTION_TRANSITIONS,
    require_execution_transition,
)


def test_terminal_states_only_leave_through_retry() -> None:
    assert ExecutionStatus.SUCCEEDED not in EXECUTION_TRANSITIONS
    assert ExecutionStatus.CANCELLED not in EXECUTION_TRANSITIONS
    assert EXECUTION_TRANSITIONS[ExecutionStatus.FAILED] == {
        ExecutionStatus.QUEUED
    }


def test_disallowed_transition_raises() -> None:
    require_execution_transition("RUNNING", ExecutionStatus.SUCCEEDED)
    with pytest.raises(InvalidStateTransitionError, match="SUCCEEDED"):
        require_execution_transition(
            ExecutionStatus.SUCCEEDED, ExecutionStatus.RUNNING
        )
    with pytest.raises(InvalidStateTransitionError):
        require_execution_transition(
            ExecutionStatus.CANCEL_REQUESTED, ExecutionStatus.SUCCEEDED
        )
