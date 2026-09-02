"""Structured intent, execution-plan, and HITL contracts for the planning Agent."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from executor_test_agent.code_policy import PlannedStep


class RequestRoute(BaseModel):
    """Choose ordinary conversation or the guarded code-execution workflow."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["CHAT", "EXECUTION"]
    reason: str = Field(min_length=1, max_length=500)


class PlannedOperation(BaseModel):
    """One append-only Executor Operation containing consecutive Steps."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=1_000)
    steps: list[PlannedStep] = Field(min_length=1, max_length=5)


class ExecutionPlan(BaseModel):
    """A complete user-reviewable plan that can be converted to Executor commands."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=2_000)
    summary: str = Field(min_length=1, max_length=2_000)
    runtime_profile: str = Field(default="default", min_length=1, max_length=128)
    operation_mode: Literal["SINGLE", "MULTI"] = "SINGLE"
    operations: list[PlannedOperation] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_operation_mode(self) -> Self:
        if self.operation_mode == "SINGLE" and len(self.operations) != 1:
            raise ValueError("SINGLE plans require exactly one Operation.")
        return self


class PlanReviewResult(BaseModel):
    """Normalized result returned by the Chat UI HITL review card."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVE", "EDIT", "REJECT"]
    plan: ExecutionPlan | None = None
    message: str | None = Field(default=None, max_length=2_000)


def plan_review_interrupt(plan: ExecutionPlan) -> dict[str, Any]:
    """Return the standard LangChain HITL request shape understood by Agent Chat UI."""

    return {
        "action_requests": [
            {
                "name": "execute_plan",
                "args": {"plan": plan.model_dump(mode="json")},
                "description": render_plan(plan),
            }
        ],
        "review_configs": [
            {
                "action_name": "execute_plan",
                "allowed_decisions": ["approve", "edit", "reject"],
            }
        ],
    }


def parse_plan_review(value: object, original: ExecutionPlan) -> PlanReviewResult:
    """Accept the standard HITL response plus a compact test/client response."""

    if not isinstance(value, dict):
        raise ValueError("Plan review response must be an object.")
    decisions = value.get("decisions")
    if isinstance(decisions, list) and len(decisions) == 1 and isinstance(decisions[0], dict):
        decision = decisions[0]
    else:
        decision = value
    decision_type = str(decision.get("type") or decision.get("action") or "").lower()
    if decision_type == "approve":
        return PlanReviewResult(decision="APPROVE", plan=original)
    if decision_type in {"reject", "cancel"}:
        return PlanReviewResult(
            decision="REJECT",
            message=str(decision.get("message") or "사용자가 실행 계획을 취소했습니다."),
        )
    if decision_type in {"edit", "modify"}:
        edited_action = decision.get("edited_action")
        if isinstance(edited_action, dict):
            args = edited_action.get("args")
            candidate = args.get("plan") if isinstance(args, dict) else None
        else:
            candidate = decision.get("plan")
        if candidate is None:
            raise ValueError("Edited plan review requires edited_action.args.plan.")
        return PlanReviewResult(
            decision="EDIT",
            plan=ExecutionPlan.model_validate(candidate),
            message=(str(decision["message"]) if decision.get("message") else None),
        )
    raise ValueError("Plan review decision must be approve, edit, or reject.")


def render_plan(plan: ExecutionPlan) -> str:
    """Render a concise plan for both the interrupt card and conversation history."""

    lines = [
        f"목표: {plan.objective}",
        f"실행 방식: {plan.operation_mode}",
        f"런타임 프로필: {plan.runtime_profile}",
        f"요약: {plan.summary}",
    ]
    for operation_index, operation in enumerate(plan.operations, start=1):
        lines.append(f"Operation {operation_index}: {operation.title} — {operation.rationale}")
        for step_index, step in enumerate(operation.steps, start=1):
            lines.append(
                f"  {step_index}. [{step.skill_name}/{step.tool_name}] {step.code.strip()}"
            )
    return "\n".join(lines)
