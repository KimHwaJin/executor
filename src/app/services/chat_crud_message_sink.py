"""Route LangGraph output to Message and AgentRunLog CRUD services.

화면 표시용 핵심 결과는 Message CRUD로, 각 Agent의 전체 결과는
AgentRunLog CRUD로 전달합니다. 이 모듈 자체는 테이블을 직접 수정하지 않습니다.

It deliberately avoids HTTP and keeps graph nodes independent from CRUD
internals.  The caller provides the DB session and user id at the graph
boundary, then persists newly emitted graph messages after each graph step.
"""

from __future__ import annotations

import json
from inspect import signature
from typing import Any, TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

if TYPE_CHECKING:
    from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
else:
    AnalysisWorkflowState = dict


# 화면에 표시하는 Graph Agent 결과는 일반 채팅의 AI 답변으로 취급합니다.
# 각 Agent의 전체 원문은 별도로 agent_run_logs에 저장됩니다.
DEFAULT_AGENT_MESSAGE_TYPE = "agent"


def _require_str(state: AnalysisWorkflowState, key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required to persist a chat message")
    return value.strip()


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return UUID(text)


def _require_uuid(state: AnalysisWorkflowState, key: str) -> UUID:
    return UUID(_require_str(state, key))


def _model_fields(model_cls: Any) -> set[str]:
    fields = getattr(model_cls, "model_fields", None)
    if isinstance(fields, dict):
        return set(fields)
    fields = getattr(model_cls, "__fields__", None)
    if isinstance(fields, dict):
        return set(fields)
    return set()


def _message_create_payload_kwargs(
    message_create_cls: Any,
    data: dict[str, Any],
) -> dict[str, Any]:
    fields = _model_fields(message_create_cls)
    if not fields:
        return data
    return {key: value for key, value in data.items() if key in fields}


def _compact_value(value: Any) -> str:
    """화면용 짧은 값 표현. 원본 JSON은 ``content``에 별도로 보존합니다."""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "예" if value else "아니오"
    return str(value)


def _content_text(content: Any, *, agent_name: str | None = None) -> str:
    """Agent 구조화 결과를 채팅 화면에 표시할 문장으로 투영합니다.

    ``content``에는 손실 없이 JSON을 저장하고, ``content_text``에는 사람이
    바로 읽을 수 있는 핵심 문장만 저장합니다. 알 수 없는 dict를 JSON 문자열로
    덤프하지 않는 것이 핵심입니다.
    """
    if isinstance(content, str):
        text = content.strip()
        # AgentChat 호환 계층이 구조화 출력을 JSON 문자열로 감싸도 화면에는
        # answer/message/final_response 같은 사람이 읽을 핵심 문장만 투영한다.
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                pass
            else:
                return _content_text(parsed, agent_name=agent_name)
        return text
    if isinstance(content, list):
        texts = [
            _content_text(item, agent_name=agent_name)
            for item in content
            if isinstance(item, (str, dict))
        ]
        return "\n".join(text for text in texts if text)
    if not isinstance(content, dict):
        return str(content).strip() if content is not None else ""

    # Agent가 명시적으로 제공한 사용자용 문장을 최우선으로 사용합니다.
    for key in ("message", "answer", "final_response"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # HITL 사용자 응답은 원본 payload 대신 의미가 드러나는 짧은 문장으로 만듭니다.
    if isinstance(content.get("objective"), str):
        return f"분석 목적: {content['objective'].strip()}"
    datasets = content.get("datasets")
    if isinstance(datasets, list):
        labels = []
        for dataset in datasets:
            if not isinstance(dataset, dict):
                continue
            dataset_id = dataset.get("dataset_id") or dataset.get("name") or "데이터"
            role = str(dataset.get("role") or "").upper()
            labels.append(f"{dataset_id}{f'({role})' if role else ''}")
        count = content.get("data_count", len(datasets))
        suffix = f": {', '.join(labels)}" if labels else ""
        return f"분석 데이터 {count}건을 선택했습니다{suffix}."
    answers = content.get("answers")
    if isinstance(answers, dict):
        summary = ", ".join(
            f"{key}={_compact_value(value)}" for key, value in answers.items()
        )
        return f"추가 정보를 입력했습니다: {summary}." if summary else "추가 정보를 입력했습니다."
    if "approved" in content:
        if content.get("approved"):
            return "Workflow를 승인했습니다."
        feedback = str(content.get("feedback") or "").strip()
        return f"Workflow 수정을 요청했습니다: {feedback}" if feedback else "Workflow 승인을 거절했습니다."

    # 같은 status라도 Agent별 출력 스키마의 의미가 다르므로 전용 projection을 우선한다.
    status = content.get("status")
    if agent_name == "workflow_recommender" and status == "candidate_search_complete":
        result = content.get("result") if isinstance(content.get("result"), dict) else {}
        if result.get("recommendation_available"):
            recommendation = result.get("recommendation") if isinstance(result.get("recommendation"), dict) else {}
            reason = str(recommendation.get("reason") or "").strip()
            score = recommendation.get("similarity_score")
            suffix = f" (유사도 {score:.2f})" if isinstance(score, (int, float)) else ""
            return f"추천 가능한 기존 Workflow를 찾았습니다{suffix}.{f' {reason}' if reason else ''}"
        reason = str(result.get("no_match_reason") or "").strip()
        return reason or "조건에 맞는 기존 Workflow가 없어 새 Workflow를 생성합니다."
    if agent_name == "workflow_candidate_collector" and status == "candidate_added":
        candidate_id = str(content.get("candidate_id") or "새 후보")
        origin = "새로 생성한" if content.get("origin") == "generated" else "추천된"
        workflow_status = str(content.get("workflow_status") or "").strip()
        status_labels = {"needs_input": "추가 정보 필요", "ready": "승인 준비 완료"}
        suffix = f" 상태: {status_labels.get(workflow_status, workflow_status)}." if workflow_status else ""
        return f"{origin} Workflow 후보 `{candidate_id}`를 목록에 추가했습니다.{suffix}"

    # 분류 Agent 결과는 코드값만 노출하지 않고 판단 이유를 함께 표시합니다.
    if isinstance(content.get("route"), str):
        labels = {
            "analysis": "데이터 분석 요청",
            "faq": "일반 질문",
            "file_lookup": "파일 조회 요청",
            "revise_workflow": "Workflow 수정 요청",
            "reselect_data": "데이터 재선택 요청",
            "cancel": "취소 요청",
        }
        route = content["route"]
        reason = str(content.get("reason") or "").strip()
        return f"요청을 {labels.get(route, route)}으로 판단했습니다.{f' {reason}' if reason else ''}"
    if isinstance(content.get("intent"), str):
        labels = {
            "failure_prediction": "불량 예측",
            "root_cause": "원인 분석",
            "data_drift": "데이터 변화 탐지",
        }
        intent = content["intent"]
        reason = str(content.get("reason") or "").strip()
        return f"분석 목적을 {labels.get(intent, intent)}으로 분류했습니다.{f' {reason}' if reason else ''}"

    action = content.get("action")
    if action == "continue" and isinstance(content.get("user_request"), str):
        return content["user_request"].strip()
    if action == "cancel":
        return "대화를 종료했습니다."
    if isinstance(status, str):
        status_messages = {
            "cancelled": "요청을 취소했습니다.",
            "success": "작업을 완료했습니다.",
            "error": "작업 처리 중 오류가 발생했습니다.",
            "running": "작업을 실행하고 있습니다.",
            "ready": "Workflow가 준비되었습니다.",
            "needs_input": "Workflow 완성을 위한 추가 정보가 필요합니다.",
        }
        return status_messages.get(status, f"현재 상태: {status}")

    # 중첩 payload에서도 명시적인 사용자용 문장만 재귀적으로 찾습니다.
    for value in content.values():
        if isinstance(value, dict):
            nested = _content_text(value, agent_name=agent_name)
            if nested and not nested.endswith("결과가 저장되었습니다."):
                return nested
    return f"{agent_name or 'Agent'} 결과가 저장되었습니다. 자세한 내용은 JSON에서 확인할 수 있습니다."


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [{"type": "json", "data": content}]


def _message_type(message: dict[str, Any], *, agent_message_type: str) -> str:
    role = message.get("role")
    if role == "user":
        return "user"
    if role == "assistant":
        return "assistant"
    if role == "system":
        return "system"
    if role == "tool":
        return "tool"
    if role == "agent":
        return agent_message_type
    return "tool"


def _client_request_id(
    state: AnalysisWorkflowState,
    *,
    node: str,
    event: str,
    index: int | None,
    message: dict[str, Any],
) -> UUID:
    basis = {
        "session_id": state.get("session_id"),
        "run_id": state.get("run_id"),
        "thread_id": state.get("thread_id"),
        "node": node,
        "event": event,
        "index": index,
        "role": message.get("role"),
        "name": message.get("name"),
        "content": message.get("content"),
    }
    raw = json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
    return uuid5(NAMESPACE_URL, raw)


def build_message_create_data(
    state: AnalysisWorkflowState,
    *,
    node: str,
    event: str,
    message: dict[str, Any],
    index: int | None = None,
    agent_message_type: str = DEFAULT_AGENT_MESSAGE_TYPE,
    trigger_message_id: str | UUID | None = None,
    agent_run_id: str | UUID | None = None,
    plan_id: str | UUID | None = None,
) -> dict[str, Any]:
    """Build keyword args for ``app.schemas.common.message_schema.MessageCreate``."""

    content = message.get("content")
    return {
        "session_id": _require_uuid(state, "session_id"),
        "project_id": _uuid_or_none(state.get("project_id")),
        "message_type": _message_type(
            message,
            agent_message_type=agent_message_type,
        ),
        "content_text": _content_text(
            content,
            agent_name=str(message.get("name") or "").strip() or None,
        ) or "표시할 메시지가 없습니다.",
        "content": _content_parts(content),
        "client_request_id": _client_request_id(
            state,
            node=node,
            event=event,
            index=index,
            message=message,
        ),
        "metadata": {
            "source": "dtest-agent",
            "run_id": state.get("run_id"),
            "thread_id": state.get("thread_id"),
            # These IDs link UI messages to future management tables such as
            # agent_runs, plans, and workflow_logs without changing nodes.
            "trigger_message_id": str(trigger_message_id)
            if trigger_message_id
            else state.get("trigger_message_id"),
            "agent_run_id": str(agent_run_id)
            if agent_run_id
            else state.get("agent_run_id"),
            "plan_id": str(plan_id) if plan_id else state.get("plan_id"),
            "node": node,
            "event": event,
            "message_index": index,
            "graph_role": message.get("role"),
            "graph_name": message.get("name"),
            "graph_message": message,
            "display_projection_version": 1,
        },
    }


async def _require_existing_session_for_message(
    db: Any,
    *,
    user_id: UUID,
    session_id: UUID,
    project_id: UUID | None,
) -> None:
    from app.repositories.session_repository import SessionRepository

    session = await SessionRepository.get_active_by_user(
        db,
        user_id=user_id,
        session_id=session_id,
    )
    if session is None:
        raise ValueError(
            "graph message persistence requires an existing session owned by "
            "the given user_id"
        )
    if project_id is not None and session.project_id != project_id:
        raise ValueError("graph message project_id does not match session.project_id")


async def _call_message_create(
    message_service: Any,
    db: Any,
    *,
    user_id: UUID,
    payload: Any,
    session_id: UUID,
    commit: bool = True,
) -> Any:
    parameters = signature(message_service.create).parameters
    if "session_id" in parameters:
        return await message_service.create(db, user_id, payload, session_id)
    kwargs = {"commit": commit} if "commit" in parameters else {}
    return await message_service.create(db, user_id, payload, **kwargs)


async def save_graph_message(
    db: Any,
    state: AnalysisWorkflowState,
    *,
    node: str,
    event: str,
    message: dict[str, Any],
    index: int | None = None,
    user_id: str | UUID | None = None,
    agent_message_type: str = DEFAULT_AGENT_MESSAGE_TYPE,
    trigger_message_id: str | UUID | None = None,
    agent_run_id: str | UUID | None = None,
    plan_id: str | UUID | None = None,
) -> Any:
    """Save one graph message using ``MessageService.create``.

    ``MessageService.create`` is expected to be a pure message insert service.
    If that service still performs LLM generation, remove that behavior before
    calling this from graph execution.
    """

    from app.schemas.common.message_schema import MessageCreate
    from app.services.message_service import MessageService

    resolved_user_id = user_id or _require_str(state, "user_id")
    resolved_user_uuid = UUID(str(resolved_user_id))
    target_session_id = _require_uuid(state, "session_id")
    target_project_id = _uuid_or_none(state.get("project_id"))
    payload_data = build_message_create_data(
        state,
        node=node,
        event=event,
        message=message,
        index=index,
        agent_message_type=agent_message_type,
        trigger_message_id=trigger_message_id,
        agent_run_id=agent_run_id,
        plan_id=plan_id,
    )
    payload = MessageCreate(
        **_message_create_payload_kwargs(MessageCreate, payload_data)
    )
    payload_session_id = getattr(payload, "session_id", None)
    if payload_session_id is None and "session_id" in _model_fields(MessageCreate):
        raise ValueError(
            "graph message persistence requires session_id; "
            f"state_session_id={state.get('session_id')!r}, "
            f"payload={payload.model_dump(mode='json')}"
        )
    await _require_existing_session_for_message(
        db,
        user_id=resolved_user_uuid,
        session_id=target_session_id,
        project_id=target_project_id,
    )
    atomic_event = bool(agent_run_id) and "commit" in signature(MessageService.create).parameters
    result = await _call_message_create(
        MessageService,
        db,
        user_id=resolved_user_uuid,
        payload=payload,
        session_id=target_session_id,
        commit=not atomic_event,
    )
    result_session_id = getattr(result, "session_id", target_session_id)
    result_session_created = bool(getattr(result, "session_created", False))
    if result_session_created or result_session_id != target_session_id:
        raise RuntimeError(
            "MessageService.create created or switched sessions during graph "
            f"persistence: expected={target_session_id}, actual={result_session_id}, "
            f"session_created={result_session_created}"
        )
    if atomic_event:
        # Message + public event commit together. Session lock from MessageService
        # also serializes the duplicate check on a retried graph step.
        from sqlalchemy import select
        from app.models import TaskEventModel
        from app.services.task_event_service import TaskEventService

        run_uuid = UUID(str(agent_run_id))
        existing = await db.scalar(select(TaskEventModel.task_event_id).where(
            TaskEventModel.run_id == run_uuid,
            TaskEventModel.event_type == "message.completed",
            TaskEventModel.payload["message_id"].astext == str(result.message.message_id),
        ).limit(1))
        if existing is None:
            channel = "final" if payload.message_type.value in {"assistant", "user"} else "commentary"
            await TaskEventService.append_for_run(
                db, run_id=run_uuid, event_type="message.completed",
                payload={"message_id": str(result.message.message_id),
                         "role": "user" if payload.message_type.value == "user" else "assistant",
                         "channel": channel,
                         "content": [{"type": "text", "text": result.message.content_text}]},
                commit=False,
            )
        await db.commit()
    return result


async def save_new_graph_messages(
    db: Any,
    state: AnalysisWorkflowState,
    saved_count: int = 0,
    *,
    user_id: str | UUID | None = None,
    node: str = "graph_stream",
    event: str = "message_emitted",
    agent_message_type: str = DEFAULT_AGENT_MESSAGE_TYPE,
) -> int:
    """Save ``state["messages"][saved_count:]`` and return the new count."""

    messages = state.get("messages") or []
    if not isinstance(messages, list):
        return saved_count
    for index, message in enumerate(messages[saved_count:], start=saved_count):
        if not isinstance(message, dict):
            continue
        await save_graph_message(
            db,
            state,
            node=message.get("metadata", {}).get("node", node),
            event=message.get("metadata", {}).get("event", event),
            message=message,
            index=index,
            user_id=user_id,
            agent_message_type=agent_message_type,
        )
    return len(messages)


async def save_agent_run_log(
    db: Any,
    *,
    run_id: str | UUID,
    event_key: str,
    node: str,
    event: str,
    kind: str,
    payload: dict[str, Any],
) -> Any:
    """Graph output을 AgentRunLog CRUD로 전달합니다; 이 모듈은 라우팅만 담당합니다."""

    from app.services.agent_run_log_service import AgentRunLogService

    return await AgentRunLogService.create(
        db,
        run_id=UUID(str(run_id)),
        event_key=event_key,
        agent_name=str(payload.get("name") or node) if payload else node,
        node=node,
        event=event,
        kind=kind,
        payload=payload,
    )


__all__ = [
    "build_message_create_data",
    "save_graph_message",
    "save_new_graph_messages",
    "save_agent_run_log",
]

