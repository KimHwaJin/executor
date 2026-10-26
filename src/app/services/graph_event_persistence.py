"""Dispatch graph state changes to UI and management persistence targets.

Target tables are expected to evolve, so graph nodes should not call table
services directly.  This module turns state deltas into normalized events and
routes them to handlers:

* messages: UI-visible conversation messages.
* agent_runs: one agent execution and agent-level internal results.
* plans: generated execution plans/notebook plans.
* workflow_logs: workflow recommendation/generation/approval/executor history.

Messages와 agent run logs는 실제 CRUD handler가 저장합니다. Plans와 workflow
logs는 해당 테이블 계약이 확정될 때까지 deferred handler로 유지합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import UUID

from app.services.chat_crud_message_sink import save_agent_run_log, save_graph_message


@dataclass(frozen=True)
class GraphPersistenceContext:
    """Stable relational context copied onto every persisted graph event."""

    user_id: UUID
    session_id: str
    trigger_message_id: str | None = None
    agent_run_id: str | None = None
    plan_id: str | None = None
    thread_id: str | None = None
    request_id: str | None = None

    @classmethod
    def from_state(
        cls,
        state: dict[str, Any],
        *,
        user_id: UUID,
        trigger_message_id: str | UUID | None = None,
        agent_run_id: str | UUID | None = None,
        plan_id: str | UUID | None = None,
    ) -> "GraphPersistenceContext":
        session_id = state.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required to persist graph events")
        return cls(
            user_id=user_id,
            session_id=session_id,
            trigger_message_id=_string_or_none(
                trigger_message_id
                or state.get("trigger_message_id")
                or state.get("message_id")
            ),
            agent_run_id=_string_or_none(agent_run_id or state.get("agent_run_id")),
            plan_id=_string_or_none(plan_id or state.get("plan_id")),
            thread_id=_string_or_none(state.get("thread_id")),
            request_id=_string_or_none(state.get("request_id")),
        )


@dataclass(frozen=True)
class GraphPersistenceCursor:
    """Tracks already-persisted parts of append/update-oriented graph state."""

    message_count: int = 0
    agent_run_started: bool = False
    plan_revision: int = 0
    workflow_revision: int = 0
    notebook_saved: bool = False
    executor_submitted: bool = False
    final_response_saved: bool = False
    seen_workflow_log_keys: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class GraphEvent:
    """Normalized event extracted from LangGraph state."""

    kind: str
    target: str
    payload: dict[str, Any]
    index: int
    node: str
    event: str
    context: GraphPersistenceContext
    display_message: dict[str, Any] | None = None


@dataclass(frozen=True)
class GraphPersistenceResult:
    cursor: GraphPersistenceCursor
    persisted: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


class GraphEventHandler(Protocol):
    target: str

    async def persist(
        self,
        db: Any,
        state: dict[str, Any],
        graph_event: GraphEvent,
    ) -> Any:
        ...


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _message_target(message: dict[str, Any]) -> str:
    role = message.get("role")
    name = message.get("name")
    content = message.get("content")
    if role in {"user", "assistant", "system"}:
        return "messages"
    if name in {"faq", "file_lookup", "cancel_request"}:
        return "messages"
    if role == "agent" and name in {
        "workflow_candidate_collector", "workflow_input_resolver", "executor",
        "rule_based_notebook_generator", "conditional_decision_agent",
    }:
        return "messages"
    if name in {"workflow_generator", "workflow_recommender"}:
        return "workflow_logs"
    if isinstance(content, dict) and content.get("llm_run_id"):
        return "agent_runs"
    return "agent_runs"


def classify_graph_message(message: dict[str, Any]) -> str:
    """Return the persistence target for one graph-emitted message.

    This small public wrapper keeps routing rules testable while table schemas
    for management data are still moving.
    """

    return _message_target(message)


def _message_kind(target: str) -> str:
    if target == "messages":
        return "display_message"
    if target == "workflow_logs":
        return "workflow_log"
    return "agent_run_log"


def _display_message_for(message: dict[str, Any]) -> dict[str, Any] | None:
    if _message_target(message) == "messages":
        return _crud_message_payload(message)
    return None


def _crud_message_payload(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("role") != "assistant":
        return message
    # FAQ/파일조회/최종 리포트는 사용자에게 직접 답하는 LLM 응답이다.
    # 그 밖의 named assistant 출력은 Agent의 중간 작업 결과로 구분한다.
    if message.get("name") in {"faq", "file_lookup", "report_writer", "executor_result"}:
        return message
    payload = dict(message)
    payload["role"] = "agent"
    return payload


def _event_key(node: str, event: str, revision: int | str | None = None) -> str:
    return f"{node}:{event}:{revision if revision is not None else ''}"


def _append_event_once(
    events: list[GraphEvent],
    seen_keys: set[str],
    *,
    key: str,
    kind: str,
    target: str,
    payload: dict[str, Any],
    node: str,
    event: str,
    context: GraphPersistenceContext,
) -> None:
    if key in seen_keys:
        return
    seen_keys.add(key)
    events.append(
        GraphEvent(
            kind=kind,
            target=target,
            payload=payload,
            index=len(events),
            node=node,
            event=event,
            context=context,
        )
    )


def extract_graph_events(
    state: dict[str, Any],
    cursor: GraphPersistenceCursor,
    *,
    context: GraphPersistenceContext,
    default_node: str = "graph_stream",
    default_event: str = "message_emitted",
) -> tuple[list[GraphEvent], GraphPersistenceCursor]:
    """Extract state deltas as table-targeted events.

    The extractor treats ``state["messages"]`` as append-only and selected
    graph objects as versioned/single-shot signals.  Add new fields here when
    new management tables become stable.
    """

    events: list[GraphEvent] = []
    seen_keys = set(cursor.seen_workflow_log_keys)

    if context.agent_run_id and not cursor.agent_run_started:
        _append_event_once(
            events,
            seen_keys,
            key=_event_key("agent_run", "started", context.agent_run_id),
            kind="agent_run",
            target="agent_runs",
            payload={
                "status": "started",
                "user_request": state.get("user_request"),
                "routing_context": state.get("routing_context"),
            },
            node="agent_run",
            event="started",
            context=context,
        )

    messages = state.get("messages") or []
    message_count = cursor.message_count
    if isinstance(messages, list):
        for message_index, message in enumerate(
            messages[cursor.message_count :],
            start=cursor.message_count,
        ):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "user" and context.trigger_message_id:
                # API가 이미 저장하고 Task에 연결한 최초 User Message는 중복 저장하지 않는다.
                # checkpoint가 과거 메시지를 함께 반환해도 API가 원본 User Message를
                # 소유하므로 graph projection에서는 User 역할을 다시 저장하지 않는다.
                continue
            metadata = (
                message.get("metadata")
                if isinstance(message.get("metadata"), dict)
                else {}
            )
            normalized_message = _crud_message_payload(message)
            if message.get("role") == "assistant":
                # LangGraph 노드는 assistant 형태로 결과를 내보내지만, 모든 Agent 결과
                # 원문은 append-only agent_run_logs에 남기고 화면용 Message도 별도로 쓴다.
                events.append(
                    GraphEvent(
                        kind="agent_run_log",
                        target="agent_runs",
                        payload=message,
                        index=message_index,
                        node=metadata.get("node", message.get("name") or default_node),
                        event=metadata.get("event", default_event),
                        context=context,
                    )
                )
                events.append(
                    GraphEvent(
                        kind="display_message",
                        target="messages",
                        payload=normalized_message,
                        index=message_index,
                        node=metadata.get("node", message.get("name") or default_node),
                        event=metadata.get("event", default_event),
                        context=context,
                        display_message=normalized_message,
                    )
                )
                continue
            target = _message_target(message)
            if message.get("role") == "agent":
                # 모든 Agent 결과 원문은 agent_run_logs에 보존합니다.
                events.append(
                    GraphEvent(
                        kind="agent_run_log",
                        target="agent_runs",
                        payload=message,
                        index=message_index,
                        node=metadata.get("node", message.get("name") or default_node),
                        event=metadata.get("event", default_event),
                        context=context,
                    )
                )
                # Workflow Agent가 만든 상태 요약은 관리 로그뿐 아니라 화면에도
                # 보여야 하므로 동일한 핵심 payload를 Message CRUD에도 전달합니다.
                if target == "workflow_logs":
                    events.append(
                        GraphEvent(
                            kind="display_message",
                            target="messages",
                            payload=message,
                            index=message_index,
                            node=metadata.get(
                                "node", message.get("name") or default_node
                            ),
                            event=metadata.get("event", default_event),
                            context=context,
                            display_message=message,
                        )
                    )
                # 화면/Workflow에도 필요한 Agent 결과는 아래에서 한 번 더 분기합니다.
                if target == "agent_runs":
                    continue
            events.append(
                GraphEvent(
                    kind=_message_kind(target),
                    target=target,
                    payload=(
                        _crud_message_payload(message)
                        if target == "messages"
                        else message
                    ),
                    index=message_index,
                    node=metadata.get("node", default_node),
                    event=metadata.get("event", default_event),
                    context=context,
                    display_message=_display_message_for(message),
                )
            )
        message_count = len(messages)

    workflow_revision = int(state.get("workflow_revision") or 0)
    plan_revision = int(state.get("plan_revision") or workflow_revision or 0)
    workflow = state.get("workflow")
    if isinstance(workflow, dict) and workflow and workflow_revision > cursor.workflow_revision:
        _append_event_once(
            events,
            seen_keys,
            key=_event_key("workflow", "generated", workflow_revision),
            kind="workflow_log",
            target="workflow_logs",
            payload={
                "workflow_revision": workflow_revision,
                "workflow_status": state.get("workflow_status"),
                "workflow_origin": state.get("workflow_origin"),
                "workflow": workflow,
            },
            node="workflow",
            event="generated",
            context=context,
        )

    if state.get("notebook") and not cursor.notebook_saved:
        _append_event_once(
            events,
            seen_keys,
            key=_event_key("plan", "notebook_generated", context.agent_run_id),
            kind="plan",
            target="plans",
            payload={
                "notebook": state.get("notebook"),
                "artifact_files": state.get("artifact_files", {}),
            },
            node="plan",
            event="notebook_generated",
            context=context,
        )

    if state.get("executor_submit_response") and not cursor.executor_submitted:
        _append_event_once(
            events,
            seen_keys,
            key=_event_key("workflow", "executor_submitted", context.agent_run_id),
            kind="workflow_log",
            target="workflow_logs",
            payload={
                "execution_id": state.get("execution_id"),
                "version_id": state.get("workflow_version_id"),
                "execution_mode": state.get("execution_mode"),
                "executor_request_path": state.get("executor_request_path"),
                "execution_steps": state.get("execution_steps", []),
                "executor_submit_response": state.get("executor_submit_response"),
            },
            node="workflow",
            event="executor_submitted",
            context=context,
        )

    final_response = state.get("final_response")
    if isinstance(final_response, dict) and final_response and not cursor.final_response_saved:
        _append_event_once(
            events,
            seen_keys,
            key=_event_key("agent_run", "completed", context.agent_run_id),
            kind="agent_run",
            target="agent_runs",
            payload=final_response,
            node="agent_run",
            event="completed",
            context=context,
        )

    next_cursor = replace(
        cursor,
        message_count=message_count,
        agent_run_started=cursor.agent_run_started or bool(context.agent_run_id),
        plan_revision=max(cursor.plan_revision, plan_revision),
        workflow_revision=max(cursor.workflow_revision, workflow_revision),
        notebook_saved=cursor.notebook_saved or bool(state.get("notebook")),
        executor_submitted=cursor.executor_submitted
        or bool(state.get("executor_submit_response")),
        final_response_saved=cursor.final_response_saved
        or bool(state.get("final_response")),
        seen_workflow_log_keys=frozenset(seen_keys),
    )
    return events, next_cursor


class MessageGraphEventHandler:
    """Persist only UI-visible graph events into the messages table."""

    target = "messages"

    def __init__(self, *, agent_message_type: str = "agent"):
        self.agent_message_type = agent_message_type

    async def persist(
        self,
        db: Any,
        state: dict[str, Any],
        graph_event: GraphEvent,
    ) -> Any:
        message = graph_event.display_message or graph_event.payload
        result = await save_graph_message(
            db,
            state,
            node=graph_event.node,
            event=graph_event.event,
            message=message,
            index=graph_event.index,
            user_id=graph_event.context.user_id,
            agent_message_type=self.agent_message_type,
            trigger_message_id=graph_event.context.trigger_message_id,
            agent_run_id=graph_event.context.agent_run_id,
            plan_id=graph_event.context.plan_id,
        )
        if (
            graph_event.payload.get("role") == "user"
            and graph_event.context.agent_run_id
        ):
            # E03-T02: persistence 경계가 Message CRUD 결과를 Task/Run 원본과 연결합니다.
            from app.services.run_service import RunService

            await RunService.attach_trigger_message(
                db,
                run_id=UUID(graph_event.context.agent_run_id),
                message_id=result.message.message_id,
            )
        return result


class AgentRunLogGraphEventHandler:
    """각 Agent/node의 전체 결과를 agent_run_logs에 append합니다."""

    target = "agent_runs"

    async def persist(self, db: Any, state: dict[str, Any], graph_event: GraphEvent) -> Any:
        run_id = graph_event.context.agent_run_id
        if not run_id:
            raise ValueError("agent_run_id is required to persist agent logs")
        event_key = f"{graph_event.node}:{graph_event.event}:{graph_event.index}"
        return await save_agent_run_log(
            db,
            run_id=run_id,
            event_key=event_key,
            node=graph_event.node,
            event=graph_event.event,
            kind=graph_event.kind,
            payload=graph_event.payload,
        )


class StructuredRunLogGraphEventHandler:
    """Workflow/Plan 구조화 결과도 현재 Run의 append-only 로그에 보존합니다."""

    def __init__(self, target: str):
        self.target = target

    async def persist(self, db: Any, state: dict[str, Any], graph_event: GraphEvent) -> Any:
        run_id = graph_event.context.agent_run_id
        if not run_id:
            raise ValueError("agent_run_id is required to persist structured logs")
        discriminator = (
            graph_event.payload.get("workflow_revision")
            or graph_event.payload.get("execution_id")
            or graph_event.context.plan_id
            or graph_event.index
            or "0"
        )
        return await save_agent_run_log(
            db,
            run_id=run_id,
            event_key=f"{graph_event.node}:{graph_event.event}:{discriminator}",
            node=graph_event.node,
            event=graph_event.event,
            kind=graph_event.kind,
            payload=graph_event.payload,
        )


class DeferredGraphEventHandler:
    """Placeholder for management tables whose schemas are not final yet."""

    def __init__(self, target: str):
        self.target = target

    async def persist(
        self,
        db: Any,
        state: dict[str, Any],
        graph_event: GraphEvent,
    ) -> dict[str, Any]:
        return {
            "deferred": True,
            "target": self.target,
            "kind": graph_event.kind,
            "node": graph_event.node,
            "event": graph_event.event,
            "session_id": graph_event.context.session_id,
            "trigger_message_id": graph_event.context.trigger_message_id,
            "agent_run_id": graph_event.context.agent_run_id,
            "plan_id": graph_event.context.plan_id,
        }


class GraphPersistenceDispatcher:
    """Route extracted graph events to target-specific persistence handlers."""

    def __init__(self, handlers: list[GraphEventHandler]):
        self.handlers = {handler.target: handler for handler in handlers}

    @classmethod
    def default(cls, *, agent_message_type: str = "agent") -> "GraphPersistenceDispatcher":
        return cls(
            [
                MessageGraphEventHandler(agent_message_type=agent_message_type),
                AgentRunLogGraphEventHandler(),
                # 별도 management table 확정 전에도 UI와 감사 로그에서 JSON을
                # 잃지 않도록 append-only agent_run_logs에 구조화 결과를 저장합니다.
                StructuredRunLogGraphEventHandler("plans"),
                StructuredRunLogGraphEventHandler("workflow_logs"),
            ]
        )

    async def persist_state_delta(
        self,
        db: Any,
        state: dict[str, Any],
        cursor: GraphPersistenceCursor,
        *,
        context: GraphPersistenceContext,
    ) -> GraphPersistenceResult:
        events, next_cursor = extract_graph_events(
            state,
            cursor,
            context=context,
        )
        persisted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for graph_event in events:
            handler = self.handlers.get(graph_event.target)
            if handler is None:
                skipped.append(
                    {
                        "target": graph_event.target,
                        "kind": graph_event.kind,
                        "index": graph_event.index,
                        "reason": "no_handler",
                    }
                )
                continue
            result = await handler.persist(db, state, graph_event)
            persisted.append(
                {
                    "target": graph_event.target,
                    "kind": graph_event.kind,
                    "index": graph_event.index,
                    "result": result,
                }
            )

        return GraphPersistenceResult(
            cursor=next_cursor,
            persisted=persisted,
            skipped=skipped,
        )


__all__ = [
    "DeferredGraphEventHandler",
    "AgentRunLogGraphEventHandler",
    "GraphEvent",
    "GraphEventHandler",
    "GraphPersistenceContext",
    "GraphPersistenceCursor",
    "GraphPersistenceDispatcher",
    "GraphPersistenceResult",
    "MessageGraphEventHandler",
    "classify_graph_message",
    "extract_graph_events",
]
