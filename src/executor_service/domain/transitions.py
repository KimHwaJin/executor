"""Allowed Execution status transitions.

API commands (domain methods) and Workers (ORM updates) both check this one
table before changing an Execution's status, so a rule changes in one place.
"""

from executor_service.domain.enums import ExecutionStatus as S
from executor_service.domain.errors import InvalidStateTransitionError

EXECUTION_TRANSITIONS: dict[S, frozenset[S]] = {
    # Claimed, cancelled, or failed when a retained retry cannot resume.
    S.QUEUED: frozenset({S.RUNNING, S.CANCEL_REQUESTED, S.FAILED}),
    S.RUNNING: frozenset(
        {
            S.SUCCEEDED,
            S.FAILED,
            S.WAITING_FOR_OPERATION,  # A MULTI Operation finished.
            S.CANCEL_REQUESTED,
            S.QUEUED,  # A retained retry waits for its Runtime Target.
        }
    ),
    S.WAITING_FOR_OPERATION: frozenset(
        {S.QUEUED, S.FINALIZING, S.CANCEL_REQUESTED, S.FAILED}
    ),
    S.FINALIZING: frozenset({S.RUNNING, S.CANCEL_REQUESTED, S.FAILED}),
    # FAILED when the maximum-runtime audit, not a caller, requested it.
    S.CANCEL_REQUESTED: frozenset({S.CANCELLED, S.FAILED}),
    S.FAILED: frozenset({S.QUEUED}),  # Explicit SINGLE retry.
}


def require_execution_transition(current: S | str, target: S) -> None:
    current = S(current)
    if target not in EXECUTION_TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransitionError(
            f"Execution cannot move from {current} to {target}."
        )
