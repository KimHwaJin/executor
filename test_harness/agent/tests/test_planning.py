"""Structured planning and standard HITL contract tests."""

import pytest
from pydantic import ValidationError

from executor_test_agent.planning import (
    ExecutionPlan,
    parse_plan_review,
    plan_review_interrupt,
)


def _plan(code: str = "print(sum(range(1, 11)))") -> ExecutionPlan:
    return ExecutionPlan.model_validate(
        {
            "objective": "1부터 10까지의 합계를 계산한다.",
            "summary": "합계를 계산하고 출력한다.",
            "runtime_profile": "basic",
            "operation_mode": "SINGLE",
            "operations": [
                {
                    "title": "합계 계산",
                    "rationale": "요청한 값을 Python으로 계산한다.",
                    "steps": [
                        {
                            "skill_name": "eda",
                            "tool_name": "sum_values",
                            "code": code,
                        }
                    ],
                }
            ],
        }
    )


def test_plan_review_uses_standard_agent_chat_ui_shape() -> None:
    request = plan_review_interrupt(_plan())

    assert request["action_requests"][0]["name"] == "execute_plan"
    assert request["review_configs"] == [
        {
            "action_name": "execute_plan",
            "allowed_decisions": ["approve", "edit", "reject"],
        }
    ]


def test_plan_review_supports_approve_edit_and_reject() -> None:
    original = _plan()
    approved = parse_plan_review({"decisions": [{"type": "approve"}]}, original)
    assert approved.decision == "APPROVE"
    assert approved.plan == original

    edited_plan = _plan("print(55)")
    edited = parse_plan_review(
        {
            "decisions": [
                {
                    "type": "edit",
                    "edited_action": {
                        "name": "execute_plan",
                        "args": {"plan": edited_plan.model_dump(mode="json")},
                    },
                }
            ]
        },
        original,
    )
    assert edited.decision == "EDIT"
    assert edited.plan == edited_plan

    rejected = parse_plan_review(
        {"decisions": [{"type": "reject", "message": "지금은 실행하지 마세요."}]},
        original,
    )
    assert rejected.decision == "REJECT"
    assert rejected.message == "지금은 실행하지 마세요."


def test_single_plan_rejects_multiple_operations() -> None:
    payload = _plan().model_dump(mode="json")
    payload["operations"].append(payload["operations"][0])

    with pytest.raises(ValidationError, match="exactly one Operation"):
        ExecutionPlan.model_validate(payload)
