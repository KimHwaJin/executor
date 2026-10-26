"""Replace LLM calls with fixed responses while the real graph nodes still run."""
from __future__ import annotations

import asyncio
import contextvars
import os
import time
import json
from copy import deepcopy
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from app.agents.orchestration.dependencies import AgentDependencies
from app.schemas.agents.orchestration_schema import (
    AnalysisIntentOutput,
    ConditionalDecisionOutput,
    RoutingOutput,
)
from app.schemas.agents.workflow_generator_schema import SkillSelectionOutput
from app.schemas.workflows.workflow_standard import StandardWorkflow


def enabled() -> bool:
    return os.environ.get("MOCK_LLM", "").strip().lower() in {"1", "true", "yes"}


def sleep_seconds() -> float:
    return max(0.0, float(os.environ.get("MOCK_LLM_SLEEP_SECONDS", "5")))


_sleep_box: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar(
    "mock_llm_sleep_box", default=None,
)
_sleep_seconds_override: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "mock_llm_sleep_seconds_override", default=None,
)


@contextmanager
def track_sleep():
    box = [0.0]
    token = _sleep_box.set(box)
    try:
        yield box
    finally:
        _sleep_box.reset(token)


@contextmanager
def override_sleep_seconds(seconds: float | None):
    token = _sleep_seconds_override.set(seconds)
    try:
        yield
    finally:
        _sleep_seconds_override.reset(token)


def _pause() -> None:
    override = _sleep_seconds_override.get()
    seconds = sleep_seconds() if override is None else max(0.0, float(override))
    if seconds:
        time.sleep(seconds)
    box = _sleep_box.get()
    if box is not None:
        box[0] += seconds


async def _apause() -> None:
    override = _sleep_seconds_override.get()
    seconds = sleep_seconds() if override is None else max(0.0, float(override))
    if seconds:
        await asyncio.sleep(seconds)
    box = _sleep_box.get()
    if box is not None:
        box[0] += seconds


class _FixedAgent:
    def __init__(self, value: Any) -> None:
        self.value = value

    def invoke(self, payload: Any) -> Any:
        _pause()
        return deepcopy(self.value)


    async def ainvoke(self, payload: Any) -> Any:
        await _apause()
        return deepcopy(self.value)


class _ConditionalAgent:
    def invoke(self, payload: Any) -> ConditionalDecisionOutput:
        _pause()
        return self._response(payload)

    async def ainvoke(self, payload: Any) -> ConditionalDecisionOutput:
        await _apause()
        return self._response(payload)

    def _response(self, payload: Any) -> ConditionalDecisionOutput:
        tools = []
        if isinstance(payload, dict):
            tools = payload.get("tools") or payload.get("conditional_tools") or []
        decisions = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            tool_id = item.get("tool_id") or item.get("id")
            if tool_id:
                decisions.append({
                    "tool_id": str(tool_id),
                    "decision": "include",
                    "reason": "Mock LLM includes the conditional tool.",
                })
        if not decisions:
            decisions = [{
                "tool_id": "mock-tool",
                "decision": "include",
                "reason": "Mock LLM includes the conditional tool.",
            }]
        return ConditionalDecisionOutput.model_validate({"decisions": decisions})


def quality_plan() -> StandardWorkflow:
    """Use the same versioned definition as the finite HTTP load client."""
    fixture = json.loads(Path(__file__).with_name('mock_workflow_scenario.json').read_text(encoding='utf-8'))
    return StandardWorkflow.model_validate(fixture['workflow'])


def create_mock_dependencies() -> AgentDependencies:
    return AgentDependencies(
        routing_agent=_FixedAgent(RoutingOutput(
            route="analysis",
            reason="Mock LLM classified the request as analysis.",
        )),
        analysis_intent_agent=_FixedAgent(AnalysisIntentOutput(
            intent="failure_prediction",
            reason="Mock LLM classified the request as failure_prediction.",
        )),
        workflow_recommender=_FixedAgent(None),
        skill_selector_agent=_FixedAgent(SkillSelectionOutput(
            skill_names=["data_load", "data_quality_check"],
        )),
        workflow_agent=_FixedAgent(quality_plan()),
        faq_agent=_FixedAgent({"answer": "Mock FAQ answer."}),
        report_agent=_FixedAgent({"content": "Mock report."}),
        conditional_decision_agent=_ConditionalAgent(),
    )
