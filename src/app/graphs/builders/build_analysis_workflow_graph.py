"""Build the dependency-injected user-agent LangGraph."""

from __future__ import annotations

import logging
from dataclasses import replace
from app.context.builder import ContextBuilder
from app.context.records import record_after
from app.graphs.nodes.session_context import make_summarize_conversation, make_analysis_question
from typing import Any

from langgraph.graph import END, START, StateGraph

from agent_config import AgentSettings
from app.agent_worker.graph_boundary import (
    ExecutionBindings,
    ExecutorBoundaryNodes,
    session_id_from,
)
from app.agents.orchestration.dependencies import AgentDependencies
from app.graphs.nodes.build_notebook_cell_code import make_build_notebook_code
from app.graphs.nodes.adaptive_execution import (
    make_build_next_adaptive_code,
    make_decide_conditional_tools,
)
from app.graphs.nodes.adaptive_executor import (
    make_cancel_adaptive_execution,
    make_finalize_adaptive_execution,
    make_submit_adaptive_operation,
)
from app.graphs.nodes.demo_artifacts import make_save_approved_workflow
from app.graphs.nodes.executor_request import (
    make_build_executor_request,
)
from app.graphs.nodes.execution_results import make_collect_execution_results
from app.graphs.nodes.redis_execution_events import (
    apply_redis_execution_event,
    make_collect_final_execution_event,
)
from app.graphs.nodes.flow_control import (
    announce_workflow_search,
    cancel_request,
)
from app.graphs.nodes.generate_report import (
    make_generate_report,
    skip_failed_execution_report,
)
from app.graphs.nodes.generate_workflow import (
    apply_missing_information_to_workflow,
    make_add_current_workflow_candidate,
    make_generate_workflow,
)
from app.graphs.nodes.recommend_workflow import make_recommend_workflow
from app.graphs.nodes.hitl import (
    await_next_user_request,
    collect_missing_information,
    make_select_workflow_candidate,
    review_workflow,
)
from app.graphs.nodes.intent_classify import make_classify_analysis_intent
from app.graphs.nodes.service_queries import make_faq_node
from app.graphs.nodes.routing import (
    ensure_analysis_task,
    make_route_user_request,
    receive_request,
)
from app.graphs.routers.orchestration_router import (
    route_approval,
    route_after_adaptive_code,
    route_after_adaptive_collection,
    route_after_adaptive_results,
    route_after_adaptive_submit,
    route_after_execution_start,
    route_after_finalize,
    route_after_static_results,
    route_redis_execution_event,
    route_report_generation,
    route_next_user_request,
    route_user_request_result,
    route_workflow_candidate_selection,
    route_workflow_status,
    route_workflow_recommendation,
)
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.services.workflow_persistence import NullWorkflowStore, WorkflowStore


logger = logging.getLogger(__name__)


def workflow_unavailable(state: AnalysisWorkflowState) -> dict:
    message = "현재 Registry Tool만으로 실행 가능한 Workflow를 만들 수 없습니다."
    return {
        "final_response": {
            "status": "workflow_unavailable",
            "workflow": state["workflow"],
            "message": message,
        },
        "messages": [
            {
                "role": "assistant",
                "name": "workflow_generator",
                "content": message,
            }
        ],
    }


def build_analysis_workflow_graph(
    deps: AgentDependencies,
    settings: AgentSettings,
    *,
    checkpointer: Any | None = None,
    submit_execution_start: Any | None = None,
    submit_execution_continue: Any | None = None,
    submit_execution_finish: Any | None = None,
    submit_execution_cancel: Any | None = None,
    execution_result_reader: Any | None = None,
    bindings: ExecutionBindings | None = None,
    workflow_store: WorkflowStore | None = None,
):
    """Compile a graph whose edges stay stable when agent internals change."""
    if isinstance(deps, AgentDependencies):
        deps = replace(deps, context_builder=deps.context_builder or ContextBuilder(settings))
    builder = StateGraph(AnalysisWorkflowState)
    recorded_nodes = {
        "select_workflow_candidate", "collect_missing_information", "apply_missing_information_to_workflow",
        "review_workflow", "save_approved_workflow", "build_executor_request",
        "collect_static_execution_results", "collect_adaptive_execution_results",
        "collect_final_execution_event", "generate_report", "skip_execution_report",
        "decide_conditional_tools", "finalize_adaptive_execution", "cancel_adaptive_execution",
    }
    def add_node(name, node):
        builder.add_node(name, record_after(node) if name in recorded_nodes else node)

    selected_workflow_store = workflow_store or NullWorkflowStore()
    boundary = ExecutorBoundaryNodes(bindings, settings.executor_tenant_id)  # type: ignore[arg-type]

    async def register_execution(state, config):
        if bindings is None:
            raise RuntimeError(
                "ExecutionBindings is required for Redis Executor events"
            )
        runtime_session_id = session_id_from(config)
        state_session_id = state.get("session_id")
        if state_session_id and state_session_id != runtime_session_id:
            raise ValueError(
                "state.session_id must match configurable.thread_id before "
                "registering an Executor execution"
            )
        return await boundary.register_execution(state, config)

    add_node("receive_request", receive_request)
    add_node("summarize_conversation", make_summarize_conversation(deps))
    add_node("analysis_question", make_analysis_question(deps))
    add_node("register_execution", register_execution)
    add_node("wait_executor_event", boundary.wait_executor_event)
    add_node("apply_redis_execution_event", apply_redis_execution_event)
    add_node(
        "record_static_executor_receipt", boundary.record_executor_receipt
    )
    add_node(
        "record_adaptive_executor_receipt", boundary.record_executor_receipt
    )
    add_node(
        "collect_final_execution_event",
        make_collect_final_execution_event(selected_workflow_store),
    )
    add_node(
        "record_final_executor_receipt", boundary.record_executor_receipt
    )
    add_node("route_user_request", make_route_user_request(deps))
    add_node("ensure_analysis_task", ensure_analysis_task)
    add_node(
        "classify_analysis_intent", make_classify_analysis_intent(deps)
    )

    add_node("faq", make_faq_node(deps))
    add_node("await_next_user_request", await_next_user_request)
    add_node("cancel_request", cancel_request)

    add_node("announce_workflow_search", announce_workflow_search)
    add_node("recommend_workflow", make_recommend_workflow(deps, settings))
    add_node("generate_workflow", make_generate_workflow(deps, settings))
    add_node(
        "add_generated_workflow_candidate",
        make_add_current_workflow_candidate(deps.workflow_catalog_service),
    )
    add_node(
        "select_workflow_candidate", make_select_workflow_candidate(settings)
    )
    add_node(
        "collect_missing_information", collect_missing_information
    )
    add_node(
        "apply_missing_information_to_workflow",
        apply_missing_information_to_workflow,
    )
    add_node("review_workflow", review_workflow)
    add_node("workflow_unavailable", workflow_unavailable)
    add_node("generate_report", make_generate_report(deps, settings))
    add_node("skip_execution_report", skip_failed_execution_report)
    add_node(
        "save_approved_workflow", make_save_approved_workflow(settings)
    )
    add_node(
        "build_notebook_code", make_build_notebook_code(settings)
    )
    add_node(
        "collect_adaptive_execution_results",
        make_collect_execution_results(
            settings,
            adaptive=True,
            workflow_store=selected_workflow_store,
            **(
                {"read_results": execution_result_reader}
                if execution_result_reader is not None
                else {}
            ),
        ),
    )
    add_node(
        "collect_static_execution_results",
        make_collect_execution_results(
            settings,
            adaptive=False,
            workflow_store=selected_workflow_store,
            **(
                {"read_results": execution_result_reader}
                if execution_result_reader is not None
                else {}
            ),
        ),
    )
    add_node(
        "decide_conditional_tools",
        make_decide_conditional_tools(deps, selected_workflow_store),
    )
    add_node(
        "build_next_adaptive_code",
        make_build_next_adaptive_code(settings),
    )
    add_node(
        "cancel_adaptive_execution",
        make_cancel_adaptive_execution(
            settings,
            **(
                {"submit_cancel": submit_execution_cancel}
                if submit_execution_cancel is not None
                else {}
            ),
        ),
    )
    add_node(
        "build_executor_request",
        make_build_executor_request(
            settings,
            workflow_store=selected_workflow_store,
            **(
                {"submit_start": submit_execution_start}
                if submit_execution_start is not None
                else {}
            ),
        ),
    )
    add_node(
        "submit_adaptive_operation",
        make_submit_adaptive_operation(
            settings,
            **(
                {"submit_continue": submit_execution_continue}
                if submit_execution_continue is not None
                else {}
            ),
        ),
    )
    add_node(
        "finalize_adaptive_execution",
        make_finalize_adaptive_execution(
            settings,
            **(
                {"submit_finish": submit_execution_finish}
                if submit_execution_finish is not None
                else {}
            ),
        ),
    )

    builder.add_edge(START, "receive_request")
    builder.add_edge("receive_request", "summarize_conversation")
    builder.add_edge("summarize_conversation", "route_user_request")
    builder.add_edge("analysis_question", END)
    builder.add_conditional_edges(
        "route_user_request",
        route_user_request_result,
        {
            "analysis": "ensure_analysis_task",
            "analysis_question": "analysis_question",
            "faq": "faq",
            "revise_workflow": "announce_workflow_search",
            "cancel": "cancel_request",
        },
    )
    builder.add_edge("ensure_analysis_task", "classify_analysis_intent")

    builder.add_edge("faq", "await_next_user_request")
    builder.add_conditional_edges(
        "await_next_user_request",
        route_next_user_request,
        {
            "route_user_request": "summarize_conversation",
            "cancel_request": "cancel_request",
        },
    )
    builder.add_edge("cancel_request", END)

    builder.add_edge("classify_analysis_intent", "announce_workflow_search")
    builder.add_edge("announce_workflow_search", "recommend_workflow")
    builder.add_conditional_edges("recommend_workflow", route_workflow_recommendation,
                                  {"select_workflow_candidate":"select_workflow_candidate",
                                   "generate_workflow":"generate_workflow"})
    builder.add_edge("generate_workflow", "add_generated_workflow_candidate")
    builder.add_edge(
        "add_generated_workflow_candidate", "select_workflow_candidate"
    )
    builder.add_conditional_edges(
        "select_workflow_candidate",
        route_workflow_candidate_selection,
        {
            "announce_workflow_search": "announce_workflow_search",
            "recommend_workflow": "recommend_workflow",
            "collect_missing_information": "collect_missing_information",
            "review_workflow": "review_workflow",
            "workflow_unavailable": "workflow_unavailable",
            "route_user_request": "summarize_conversation",
        },
    )
    builder.add_edge(
        "collect_missing_information", "apply_missing_information_to_workflow"
    )
    builder.add_conditional_edges(
        "apply_missing_information_to_workflow",
        route_workflow_status,
        {
            "collect_missing_information": "collect_missing_information",
            "review_workflow": "review_workflow",
            "workflow_unavailable": "workflow_unavailable",
        },
    )
    builder.add_conditional_edges(
        "review_workflow",
        route_approval,
        {
            "save_approved_workflow": "save_approved_workflow",
            "route_user_request": "summarize_conversation",
        },
    )
    builder.add_edge("workflow_unavailable", END)
    builder.add_edge("save_approved_workflow", "build_notebook_code")
    builder.add_edge("build_notebook_code", "build_executor_request")
    builder.add_conditional_edges(
        "build_executor_request",
        route_after_execution_start,
        {
            "collect_adaptive_execution_results": "collect_adaptive_execution_results",
            "collect_static_execution_results": "collect_static_execution_results",
            "register_execution": "register_execution",
            "end": END,
        },
    )
    builder.add_edge("register_execution", "wait_executor_event")
    builder.add_edge("wait_executor_event", "apply_redis_execution_event")
    builder.add_conditional_edges(
        "apply_redis_execution_event",
        route_redis_execution_event,
        {
            "collect_static_execution_results": "collect_static_execution_results",
            "collect_adaptive_execution_results": "collect_adaptive_execution_results",
            "collect_final_execution_event": "collect_final_execution_event",
        },
    )
    builder.add_conditional_edges(
        "collect_static_execution_results",
        route_after_static_results,
        {
            "record_static_executor_receipt": "record_static_executor_receipt",
            "generate_report": "generate_report",
            "skip_execution_report": "skip_execution_report",
        },
    )
    builder.add_conditional_edges(
        "record_static_executor_receipt",
        route_report_generation,
        {
            "generate_report": "generate_report",
            "skip_execution_report": "skip_execution_report",
        },
    )
    builder.add_conditional_edges(
        "collect_adaptive_execution_results",
        route_after_adaptive_collection,
        {
            "record_adaptive_executor_receipt": "record_adaptive_executor_receipt",
            "build_next_adaptive_code": "build_next_adaptive_code",
            "decide_conditional_tools": "decide_conditional_tools",
            "finalize_adaptive_execution": "finalize_adaptive_execution",
            "cancel_adaptive_execution": "cancel_adaptive_execution",
        },
    )
    builder.add_conditional_edges(
        "record_adaptive_executor_receipt",
        route_after_adaptive_results,
        {
            "skip_execution_report": "skip_execution_report",
            "build_next_adaptive_code": "build_next_adaptive_code",
            "decide_conditional_tools": "decide_conditional_tools",
            "finalize_adaptive_execution": "finalize_adaptive_execution",
            "cancel_adaptive_execution": "cancel_adaptive_execution",
        },
    )
    builder.add_edge("decide_conditional_tools", "build_next_adaptive_code")
    builder.add_conditional_edges(
        "build_next_adaptive_code",
        route_after_adaptive_code,
        {
            "submit_adaptive_operation": "submit_adaptive_operation",
            "finalize_adaptive_execution": "finalize_adaptive_execution",
        },
    )
    builder.add_conditional_edges(
        "submit_adaptive_operation",
        route_after_adaptive_submit,
        {"wait_executor_event": "wait_executor_event", "end": END},
    )
    builder.add_conditional_edges(
        "finalize_adaptive_execution",
        route_after_finalize,
        {"wait_executor_event": "wait_executor_event", "end": END},
    )
    builder.add_conditional_edges(
        "cancel_adaptive_execution",
        route_after_finalize,
        {"wait_executor_event": "wait_executor_event", "end": END},
    )
    builder.add_edge(
        "collect_final_execution_event", "record_final_executor_receipt"
    )
    builder.add_conditional_edges(
        "record_final_executor_receipt",
        route_report_generation,
        {
            "generate_report": "generate_report",
            "skip_execution_report": "skip_execution_report",
        },
    )
    builder.add_edge("generate_report", END)
    builder.add_edge("skip_execution_report", END)

    logger.debug("[DEBUG] GRAPH compile start")
    graph = builder.compile(checkpointer=checkpointer)
    logger.debug("[DEBUG] GRAPH compile complete")
    return graph


build_user_agent_graph = build_analysis_workflow_graph

__all__ = ["build_analysis_workflow_graph", "build_user_agent_graph"]
