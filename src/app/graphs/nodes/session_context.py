"""Bounded conversation summarization and read-only analysis follow-up answers."""
from __future__ import annotations

import logging

from app.context.budget import ContextBudgetExceeded, estimate_tokens, dumps, token_chunks
from app.context.builder import context_payload, conversation_turns

logger = logging.getLogger(__name__)


def make_summarize_conversation(deps):
    async def summarize_conversation(state):
        builder = deps.context_builder
        agent = deps.summary_agent
        if builder is None or agent is None:
            return {}
        turns = conversation_turns(builder.unsummarized(state))
        summary = state.get("conversation_summary", "")
        if len(turns) < 2:
            return {}
        # Protect the active turn. Summarize the oldest completed prefix until
        # the remaining history fits, even below the normal summary trigger.
        completed = turns[:-1]
        count = 0
        while count < len(completed) and estimate_tokens(completed[count:]) > builder.recent_turns_budget:
            count += 1
        if estimate_tokens([summary, turns]) > builder.summary_trigger:
            count = max(count, len(turns) - 2 if len(turns) > 2 else 1)
        old = completed[:count]
        cursor = state.get("summarized_through_message_id")
        for turn in old:
            # Summarize a complete turn in bounded text chunks. These are source
            # documents inside a user payload, never orphaned native ToolMessages.
            source = dumps(turn)
            chunks = token_chunks(source, builder.summary_chunk)
            candidate_summary = summary
            try:
                if not turn[-1].get("id"):
                    raise ValueError("Summary cursor requires a message ID")
                for index, chunk in enumerate(chunks):
                    response = await agent.ainvoke({
                        "user_request": "이전 요약과 대화 구간을 합쳐 세션 요약을 갱신하세요.",
                        "previous_summary": candidate_summary,
                        "conversation_chunk": chunk,
                        "chunk_index": index + 1,
                        "chunk_count": len(chunks),
                        "target_tokens": builder.summary_target,
                    })
                    updated = response.get("answer") if isinstance(response, dict) else None
                    if not isinstance(updated, str) or not updated.strip():
                        raise ValueError("Summary must be non-empty text")
                    if estimate_tokens(updated) > builder.summary_target:
                        response = await agent.ainvoke({
                            "user_request": "요약을 더 짧게 다시 작성하세요. 핵심 결정과 미해결 질문만 보존하세요.",
                            "previous_summary": updated,
                            "target_tokens": builder.summary_target,
                        })
                        updated = response.get("answer", "")
                        if not updated.strip() or estimate_tokens(updated) > builder.summary_target:
                            raise ValueError("Summary still exceeds its bounded target")
                    candidate_summary = updated.strip()
            except Exception:
                logger.warning("Conversation summary unavailable; preserving original messages and cursor", exc_info=True)
                break
            # Advance atomically only after every chunk of the whole turn succeeds.
            summary = candidate_summary
            cursor = turn[-1].get("id")
        if cursor and cursor != state.get("summarized_through_message_id"):
            return {"conversation_summary": summary, "summarized_through_message_id": cursor}
        return {}
    return summarize_conversation


def make_analysis_question(deps):
    async def analysis_question(state):
        ids = state.get("referenced_analysis_task_ids") or []
        records = state.get("analysis_records") or {}
        if state.get("analysis_reference_ambiguous") or not ids or any(key not in records for key in ids):
            answer = "어떤 분석을 말씀하시는지 분석 순서나 요청 내용을 알려주세요."
        else:
            agent = deps.analysis_question_agent or deps.faq_agent
            payload = context_payload(deps, state, "analysis_question", {"user_request": state["user_request"]})
            try:
                result = await agent.ainvoke(payload)
                answer = result.get("answer") if isinstance(result, dict) else None
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError("Analysis follow-up must return a non-empty answer")
            except ContextBudgetExceeded:
                answer = "참조할 분석 내용이 입력 한도를 넘습니다. 분석 하나 또는 특정 도구·결과로 질문 범위를 좁혀주세요."
        return {
            "final_response": {"status": "analysis_question_answered", "answer": answer, "task_ids": ids},
            "service_response": {"service": "analysis_question", "response": {"answer": answer}},
            "messages": [{"role": "assistant", "name": "analysis_question", "content": answer}],
        }
    return analysis_question
