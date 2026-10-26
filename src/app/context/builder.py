"""Explicit, purpose-specific selection of session context. No LLM classification."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.context.budget import ContextBudget, estimate_tokens
from app.context.records import excerpt


def message_dict(message: Any) -> dict:
    if isinstance(message, dict):
        return deepcopy(message)
    return message.model_dump(exclude_none=True)


def user_message(message: dict) -> bool:
    return message.get("role") in {"user", "human"} or message.get("type") == "human"


def conversation_turns(messages: list) -> list[list[dict]]:
    """Keep a user turn and its entire tool-call/result exchange together."""
    turns: list[list[dict]] = []
    for raw in messages:
        message = message_dict(raw)
        if user_message(message) or not turns:
            turns.append([])
        turns[-1].append(message)
    return turns


class ContextBuilder:
    def __init__(self, settings):
        self.budget = ContextBudget(settings.context_input_budget_tokens)
        self.summary_trigger = settings.context_summary_trigger_tokens
        self.recent_turns_budget = min(self.summary_trigger // 2, self.budget.input_limit // 4)
        self.analysis_list_budget = self.budget.input_limit // 16
        self.summary_target = min(1024, self.summary_trigger // 8)
        self.summary_chunk = min(self.summary_trigger // 4, self.budget.input_limit // 8)

    def unsummarized(self, state: dict) -> list:
        messages = state.get("messages") or []
        cursor = state.get("summarized_through_message_id")
        if cursor:
            for index, message in enumerate(messages):
                if message_dict(message).get("id") == cursor:
                    return messages[index + 1:]
        return messages

    def recent_turns(self, state: dict) -> list:
        messages = list(self.unsummarized(state))
        # Current question is supplied separately, exactly once.
        if messages and user_message(message_dict(messages[-1])):
            if message_dict(messages[-1]).get("content") == state.get("user_request"):
                messages.pop()
        # Only the summary cursor may retire history. If summarization failed,
        # retain the originals and let the final input budget check decide.
        return conversation_turns(messages)

    def build(self, state: dict, purpose: str, payload: dict) -> dict:
        allowed = {"routing", "analysis_question", "faq", "intent", "skill_selection",
                   "workflow_generation", "report_generation", "conditional_decision"}
        if purpose not in allowed:
            raise ValueError(f"Unknown context purpose: {purpose}")
        context: dict[str, Any] = {"purpose": purpose}
        if purpose not in {"report_generation", "conditional_decision"}:
            context.update(conversation_summary=state.get("conversation_summary", ""),
                           recent_turns=self.recent_turns(state))
        records = state.get("analysis_records") or {}
        if purpose == "routing":
            context["current_analysis_task_id"] = state.get("current_analysis_task_id")
            summaries, used = [], 0
            for ordinal, (task_id, record) in reversed(list(enumerate(records.items(), start=1))):
                item = {"task_id": task_id, "ordinal": ordinal,
                        "request": excerpt(record.get("original_request", ""), 400),
                        "execution_status": record.get("execution_status"),
                        "report_status": record.get("report_status")}
                size = estimate_tokens(item)
                if used + size > self.analysis_list_budget:
                    break
                summaries.insert(0, item)
                used += size
            context["available_analyses"] = summaries
            context["analysis_count"] = len(records)
            context["analysis_list_incomplete"] = len(summaries) < len(records)
        elif purpose == "analysis_question":
            ids = state.get("referenced_analysis_task_ids") or []
            context["analyses"] = [deepcopy(records[task_id]) for task_id in ids if task_id in records]
            # Report originals live in the existing append-only messages; references
            # allow retrieval after the active execution fields have been reset.
            report_ids = {(record.get("references") or {}).get("report_message_id")
                          for record in context["analyses"]}
            context["reports"] = [
                {"message_id": message_dict(message).get("id"),
                 "content": str(message_dict(message).get("content", ""))}
                for message in state.get("messages") or []
                if message_dict(message).get("id") in report_ids - {None}
            ]
        elif purpose in {"workflow_generation", "report_generation", "conditional_decision"}:
            # Never import parameters from an unrelated previous task automatically.
            record = records.get(state.get("task_id"), {})
            context["confirmed_inputs"] = record.get("confirmed_inputs", [])
        return {**payload, "session_context": context}


def context_payload(deps, state: dict, purpose: str, payload: dict) -> dict:
    builder = getattr(deps, "context_builder", None)
    return builder.build(state, purpose, payload) if builder is not None else payload
