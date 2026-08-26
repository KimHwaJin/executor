"""Planning Agent graph tests without an external LLM, Redis, Executor, or Jupyter."""

from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from executor_test_agent import planning_graph as graph_module
from executor_test_agent.integrations.contracts import ExecutionEventBatch
from executor_test_agent.planning import ExecutionPlan, RequestRoute
from executor_test_agent.planning_state import PlanningAgentState


def _event(
    execution_id: str,
    event_id: str,
    event_type: str,
    status: str,
    **payload: Any,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": "1.0",
        "execution_id": execution_id,
        "occurred_at": "2026-08-19T00:00:00Z",
        "payload": {
            "status": ("SUCCEEDED" if event_type == "execution.operation_completed" else status),
            **(
                {"execution_status": status}
                if event_type == "execution.operation_completed"
                else {}
            ),
            **payload,
        },
    }


def _batch(*events: dict[str, Any]) -> ExecutionEventBatch:
    return ExecutionEventBatch.model_validate({"events": list(events), "wake_event": events[-1]})


def _plan(mode: str = "SINGLE") -> ExecutionPlan:
    operations = [
        {
            "title": "값 계산",
            "rationale": "요청한 값을 계산한다.",
            "steps": [
                {
                    "skill_name": "eda",
                    "tool_name": "calculate_value",
                    "code": "value = sum(range(1, 11))\nprint(value)",
                }
            ],
        }
    ]
    if mode == "MULTI":
        operations.append(
            {
                "title": "결과 재사용",
                "rationale": "앞에서 계산한 값을 재사용한다.",
                "steps": [
                    {
                        "skill_name": "report",
                        "tool_name": "display_value",
                        "code": "print(value * 2)",
                    }
                ],
            }
        )
    return ExecutionPlan.model_validate(
        {
            "objective": "값을 계산하고 출력한다.",
            "summary": "두 단계 계산 계획" if mode == "MULTI" else "합계 계산 계획",
            "runtime_profile": "basic",
            "operation_mode": mode,
            "operations": operations,
        }
    )


class _Structured:
    def __init__(self, value: object) -> None:
        self.value = value

    async def ainvoke(self, _messages: object) -> object:
        return self.value


class _Model:
    def __init__(
        self, route: Literal["CHAT", "EXECUTION"], plan: ExecutionPlan | None = None
    ) -> None:
        self.route = route
        self.plan = plan

    def with_structured_output(self, schema: type, *, method: str) -> _Structured:
        assert method == "function_calling"
        if schema is RequestRoute:
            return _Structured(RequestRoute(intent=self.route, reason="test route"))
        if schema is ExecutionPlan and self.plan is not None:
            return _Structured(self.plan)
        raise AssertionError(f"Unexpected structured schema: {schema}")

    async def ainvoke(self, _messages: object) -> AIMessage:
        return AIMessage(content="일반 질문에 대한 답변입니다.")


async def test_planning_agent_answers_chat_without_executor_mutation(monkeypatch) -> None:
    class _ReadAgent:
        async def ainvoke(self, state):
            return {
                "messages": [
                    *state["messages"],
                    AIMessage(content="안녕하세요. 무엇을 도와드릴까요?"),
                ]
            }

    async def fake_read_tools(_settings):
        return [object()]

    monkeypatch.setattr(graph_module, "_chat_model", lambda: _Model("CHAT"))
    monkeypatch.setattr(graph_module, "load_executor_read_tools", fake_read_tools)
    monkeypatch.setattr(
        graph_module,
        "create_agent",
        lambda _model, _tools, *, system_prompt: _ReadAgent(),
    )

    result = await graph_module.graph.ainvoke(
        PlanningAgentState(messages=[HumanMessage(content="안녕")]),
        RunnableConfig(configurable={"thread_id": "planning-chat"}),
    )

    state = PlanningAgentState.model_validate(result)
    assert state.phase == "READY"
    assert state.intent == "CHAT"
    assert state.execution_id is None
    assert "안녕하세요" in str(state.messages[-1].content)


async def test_planning_agent_requires_approval_before_single_execution(monkeypatch) -> None:
    execution_id = "00000000-0000-0000-0000-000000000101"
    submitted: list[object] = []

    async def fake_submit(request, _settings):
        submitted.append(request)
        assert request.actor_id == graph_module.PLANNING_AGENT_ID
        return {
            "execution_id": execution_id,
            "operation": {"operation_id": "00000000-0000-0000-0000-000000000102"},
            "state": {"status": "QUEUED", "version": 0},
            "event_stream_start_id": "10-0",
        }

    terminal = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000103",
        "execution.completed",
        "SUCCEEDED",
    )

    class _Waiter:
        def __init__(self, *_args, **kwargs):
            assert kwargs["start_id"] == "10-0"

        async def open(self):
            pass

        async def close(self):
            pass

        async def wait_for_wakeup(self, *_args, **_kwargs):
            return _batch(terminal)

    async def fake_reconcile(_execution_id, _event_batch, _settings):
        return {
            "execution_id": execution_id,
            "status": "SUCCEEDED",
            "version": 3,
            "steps": [{"sequence": 0, "result": {"status": "SUCCEEDED"}}],
            "artifacts": [{"name": "execution.ipynb"}],
            "notebook_path": "users/u/executions/e/notebooks/execution.ipynb",
            "notebook": {"cells": []},
            "step_events": [],
            "operation_events": [],
        }

    monkeypatch.setattr(graph_module, "_chat_model", lambda: _Model("EXECUTION", _plan()))
    monkeypatch.setattr(graph_module, "submit_execution", fake_submit)
    monkeypatch.setattr(graph_module, "ExecutionEventWaiter", _Waiter)
    monkeypatch.setattr(graph_module, "reconcile_execution", fake_reconcile)

    graph = graph_module.builder.compile(checkpointer=MemorySaver())
    config = RunnableConfig(configurable={"thread_id": "planning-approval"})
    interrupted = await graph.ainvoke(
        PlanningAgentState(messages=[HumanMessage(content="1부터 10까지 합계를 실행해줘")]),
        config,
    )

    assert interrupted["phase"] == "AWAITING_APPROVAL"
    assert interrupted["__interrupt__"][0].value["action_requests"][0]["name"] == "execute_plan"
    assert submitted == []

    result = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config,
    )
    state = PlanningAgentState.model_validate(result)
    assert state.phase == "SUCCEEDED"
    assert state.execution_id == execution_id
    assert len(submitted) == 1
    assert len(state.command_receipts) == 1


async def test_planning_agent_rejects_without_submission(monkeypatch) -> None:
    async def unexpected_submit(*_args, **_kwargs):
        raise AssertionError("Rejected plan must not be submitted.")

    monkeypatch.setattr(graph_module, "_chat_model", lambda: _Model("EXECUTION", _plan()))
    monkeypatch.setattr(graph_module, "submit_execution", unexpected_submit)
    graph = graph_module.builder.compile(checkpointer=MemorySaver())
    config = RunnableConfig(configurable={"thread_id": "planning-reject"})
    await graph.ainvoke(
        PlanningAgentState(messages=[HumanMessage(content="코드를 실행해줘")]), config
    )

    result = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "reject", "message": "요청을 취소합니다."}]}),
        config,
    )
    state = PlanningAgentState.model_validate(result)
    assert state.phase == "CANCELLED"
    assert state.execution_id is None
    assert "취소" in str(state.messages[-1].content)


async def test_submit_plan_uses_the_user_edited_plan(monkeypatch) -> None:
    edited = _plan().model_copy(deep=True)
    edited.operations[0].steps[0].code = "print(55)"

    async def fake_submit(request, _settings):
        assert request.steps[0].code == "print(55)"
        return {
            "execution_id": "00000000-0000-0000-0000-000000000150",
            "operation": {"operation_id": "00000000-0000-0000-0000-000000000151"},
            "state": {"status": "QUEUED", "version": 0},
            "event_stream_start_id": "15-0",
        }

    monkeypatch.setattr(graph_module, "submit_execution", fake_submit)
    result = await graph_module.submit_plan(
        PlanningAgentState(
            messages=[HumanMessage(content="수정한 계획을 실행해줘")],
            phase="SUBMITTING",
            request_scope_id="edited-plan",
            approved_plan=edited.model_dump(mode="json"),
        )
    )

    assert result["phase"] == "WAITING_FOR_EVENT"
    assert result["execution_id"] == "00000000-0000-0000-0000-000000000150"


async def test_planning_agent_runs_every_approved_multi_operation(monkeypatch) -> None:
    execution_id = "00000000-0000-0000-0000-000000000201"
    calls: list[str] = []
    events = [
        _batch(
            _event(
                execution_id,
                "00000000-0000-0000-0000-000000000211",
                "execution.step_completed",
                "SUCCEEDED",
                result={"outputs": []},
            ),
            _event(
                execution_id,
                "00000000-0000-0000-0000-000000000212",
                "execution.operation_completed",
                "WAITING_FOR_OPERATION",
            ),
        ),
        _batch(
            _event(
                execution_id,
                "00000000-0000-0000-0000-000000000213",
                "execution.step_completed",
                "SUCCEEDED",
                result={"outputs": []},
            ),
            _event(
                execution_id,
                "00000000-0000-0000-0000-000000000214",
                "execution.operation_completed",
                "WAITING_FOR_OPERATION",
            ),
        ),
        _batch(
            _event(
                execution_id,
                "00000000-0000-0000-0000-000000000215",
                "execution.completed",
                "SUCCEEDED",
            )
        ),
    ]

    async def fake_submit(request, _settings):
        calls.append("submit")
        assert request.operation_mode == "MULTI"
        return {
            "execution_id": execution_id,
            "operation": {"operation_id": "00000000-0000-0000-0000-000000000202"},
            "state": {"status": "QUEUED", "version": 0},
            "event_stream_start_id": "20-0",
        }

    async def fake_operation(
        _execution_id,
        steps,
        _settings,
        *,
        operation_index,
        expected_version,
        first_sequence,
        actor_id,
    ):
        calls.append("operation")
        assert operation_index == 0
        assert expected_version == 2
        assert first_sequence == 1
        assert actor_id == graph_module.PLANNING_AGENT_ID
        assert steps[0].tool_name == "display_value"
        return {
            "execution_id": execution_id,
            "operation": {"operation_id": "00000000-0000-0000-0000-000000000203"},
            "state": {"status": "QUEUED", "version": 3},
            "event_stream_start_id": "21-0",
        }

    async def fake_finalize(_execution_id, _settings, *, expected_version, actor_id):
        calls.append("finalize")
        assert expected_version == 5
        assert actor_id == graph_module.PLANNING_AGENT_ID
        return {
            "execution_id": execution_id,
            "operation": None,
            "state": {"status": "FINALIZING", "version": 6},
            "event_stream_start_id": "22-0",
        }

    class _Waiter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def open(self):
            pass

        async def close(self):
            pass

        async def wait_for_wakeup(self, *_args, **_kwargs):
            return events.pop(0)

    async def fake_reconcile(_execution_id, batch, _settings):
        status = batch.wake_event.payload.get(
            "execution_status", batch.wake_event.payload["status"]
        )
        version = 2 if calls == ["submit"] else 5 if calls[-1] == "operation" else 7
        return {
            "execution_id": execution_id,
            "status": status,
            "version": version,
            "steps": [{"sequence": index} for index in range(len(calls))],
            "artifacts": ([{"name": "execution.ipynb"}] if status == "SUCCEEDED" else []),
            "notebook_path": "users/u/executions/e/notebooks/execution.ipynb",
            "notebook": {"cells": []},
            "step_events": [
                event.model_dump(mode="json")
                for event in batch.events
                if event.event_type.startswith("execution.step_")
            ],
            "operation_events": [],
        }

    monkeypatch.setattr(graph_module, "_chat_model", lambda: _Model("EXECUTION", _plan("MULTI")))
    monkeypatch.setattr(graph_module, "submit_execution", fake_submit)
    monkeypatch.setattr(graph_module, "create_execution_operation", fake_operation)
    monkeypatch.setattr(graph_module, "finalize_execution", fake_finalize)
    monkeypatch.setattr(graph_module, "ExecutionEventWaiter", _Waiter)
    monkeypatch.setattr(graph_module, "reconcile_execution", fake_reconcile)

    graph = graph_module.builder.compile(checkpointer=MemorySaver())
    config = RunnableConfig(configurable={"thread_id": "planning-multi"})
    await graph.ainvoke(
        PlanningAgentState(messages=[HumanMessage(content="두 단계로 실행해줘")]), config
    )
    result = await graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    state = PlanningAgentState.model_validate(result)

    assert state.phase == "SUCCEEDED"
    assert calls == ["submit", "operation", "finalize"]
    assert len(state.command_receipts) == 3
    assert len(state.event_history) == 5


async def test_planning_agent_reports_multi_operation_error_and_stops(monkeypatch) -> None:
    execution_id = "00000000-0000-0000-0000-000000000301"
    failed = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000302",
        "execution.step_completed",
        "FAILED",
        result={"error_message": "division by zero", "outputs": []},
    )
    waiting = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000303",
        "execution.operation_completed",
        "WAITING_FOR_OPERATION",
    )

    async def fake_reconcile(_execution_id, _batch, _settings):
        return {
            "execution_id": execution_id,
            "status": "WAITING_FOR_OPERATION",
            "version": 2,
            "steps": [
                {
                    "sequence": 0,
                    "result": {
                        "status": "FAILED",
                        "outputs": [],
                        "error_message": "division by zero",
                    },
                }
            ],
            "artifacts": [],
            "notebook_path": "users/u/executions/e/notebooks/execution.ipynb",
            "notebook": {"cells": []},
            "step_events": [failed],
            "operation_events": [],
        }

    monkeypatch.setattr(graph_module, "reconcile_execution", fake_reconcile)
    request = {
        "runtime_profile": "basic",
        "actor_id": graph_module.PLANNING_AGENT_ID,
        "user_id": "u",
        "project_id": "p",
        "session_id": "s",
        "task_id": "t",
        "operation_mode": "MULTI",
        "operation_wait_timeout_seconds": 600,
        "steps": _plan("MULTI").operations[0].steps,
        "follow_up_operations": [_plan("MULTI").operations[1].steps],
        "auto_finalize": True,
    }
    result = await graph_module.verify_execution(
        PlanningAgentState(
            messages=[HumanMessage(content="실행해줘")],
            phase="VERIFYING",
            execution_id=execution_id,
            execution_request=request,
            event_batch=_batch(failed, waiting).model_dump(mode="json"),
        )
    )

    assert result["phase"] == "FAILED"
    assert result["execution_request"] is None
    assert "오류" in str(result["messages"][0].content)
    assert "division by zero" in str(result["messages"][0].content)
