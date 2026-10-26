"""Routing/transport and legacy compiler unit tests.

Current Workflow-first scenarios live in test_workflow_first_scenario.py.
"""

from __future__ import annotations

import asyncio
import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage
from langgraph.types import Command

from agent_config import (
    ENABLED_ANALYSIS_INTENTS,
    build_local_mock_request_context,
    load_agent_settings,
)
from app.agents.orchestration.agent_interfaces import (
    JsonMessageAgentAdapter,
    LabelOnlyLLMAgent,
    SimpleLLMAgent,
    StructuredLLMAgent,
    invoke_typed,
)
from app.agents.orchestration.dependencies import AgentDependencies
from app.agents.workflow_generator.create_workflow_generator_agent import (
    create_workflow_generator_agent,
)
from app.agents.workflow_generator.data_load_steps import (
    merge_required_data_load_steps,
)
from app.agents.workflow_generator.workflow_generator_tools import (
    canonicalize_registry_tool_sources,
    load_workflow_catalog_context,
    read_skill_documents,
    validate_skill_names,
)
from app.graphs.checkpoint import compiled_in_memory_graph
from app.graphs.hitl_protocol import (
    build_hitl_request,
    hitl_request_args,
    unwrap_hitl_response,
)
from app.graphs.message_utils import append_messages_with_ids
from cli import _print_workflow_summary, _stream_graph
from app.graphs.nodes.intent_classify import make_classify_analysis_intent
from app.graphs.nodes.routing import ensure_analysis_task, receive_request
from app.graphs.nodes.generate_workflow import (
    _apply_confirmed_additional_information,
    _require_human_confirmation_for_inputs,
)
from app.graphs.nodes.generate_report import make_generate_report
from app.graphs.nodes.hitl import (
    _coerce_answers_by_input_schema,
    _workflow_candidate_description,
)
from app.graphs.visualization import build_visualization_graph, graph_to_mermaid
from app.services.workflow_recommender import WorkflowRecommender
from app.schemas.workflows.workflow_format import (
    InputDefinition,
    WorkflowGeneratorOutput,
)
from app.schemas.agents.orchestration_schema import RoutingOutput
from app.schemas.agents.workflow_generator_schema import SkillSelectionOutput


_TEST_ARTIFACTS = tempfile.TemporaryDirectory()


class RoutingAgent:
    def invoke(self, payload):
        request = payload["user_request"].lower()
        if payload.get("routing_context") == "workflow_rejected":
            if "reselect" in request or "다시 선택" in request:
                return {"route": "revise_workflow", "reason": "data change"}
            if "revise" in request or "수정" in request:
                return {"route": "revise_workflow", "reason": "workflow revision"}
            if "cancel" in request or "취소" in request:
                return {"route": "cancel", "reason": "cancel request"}
        if "faq" in request:
            return {"route": "faq", "reason": "FAQ keyword"}
        if "file" in request:
            return {"route": "faq", "reason": "unsupported file lookup"}
        return {"route": "analysis", "reason": "analysis request"}

    async def ainvoke(self, payload):
        return self.invoke(payload)


class ConstantAgent:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        return self.value

    async def ainvoke(self, payload):
        return self.invoke(payload)


class FakeExecutorSubmitter:
    def __init__(self):
        self.calls = []

    def __call__(self, settings, payload):
        self.calls.append({"settings": settings, "payload": payload})
        request_steps = payload["operation"]["spec"]["steps"]
        return {
            "status_code": 202,
            "body": {
                "execution_id": "25272eec-5c6f-416c-bd6f-f1fcb0f988f2",
                "operation": {
                    "operation_id": "executor-operation-1",
                    "steps": [
                        {
                            "sequence": step["sequence"],
                            "step_id": f"executor-cell-{step['sequence']}",
                        }
                        for step in request_steps
                    ],
                },
                "state": {
                    "status": "QUEUED",
                    "version": 1,
                },
            },
        }


class FakeExecutionBindings:
    def __init__(self):
        self.registrations = []

    async def register(self, **registration):
        self.registrations.append(registration)


class ReportAgent:
    def __init__(self):
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        return {
            "content": (
                "# 분석 리포트\n\n"
                "## 분석 내용 요약\n"
                "데이터 로드와 분석 단계가 수행되었습니다.\n\n"
                "## 실행 결과\n"
                "step 결과가 workflow와 매칭되었습니다.\n\n"
                "## 이후 작업 추천\n"
                "상세 지표를 검토하세요."
            )
        }

    async def ainvoke(self, payload):
        return self.invoke(payload)


class WorkflowAgent:
    def __init__(self):
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        selection = payload["data_selection"]
        target = payload["additional_information"].get("target_column")
        needs_input = target is None
        input_schema = {}
        inputs = {}
        provenance = {}

        input_schema["target_column"] = {
            "type": "str",
            "required": True,
            "allow_llm_inference": False,
            "validation": [],
        }
        inputs["target_column"] = target
        provenance["target_column"] = {
            "source": "user_answer" if target else None,
            "confirmed": bool(target),
        }
        unresolved = (
            [
                {
                    "name": "target_column",
                    "question": "예측 대상 컬럼명을 입력해주세요.",
                    "required_for": ["analysis"],
                }
            ]
            if needs_input
            else []
        )
        steps = [
            {
                "id": "profile_selected_data",
                "order": 1,
                "skill": "data_quality_check",
                "skill_source": "app/workflow/skills/eda/data_quality_check.md",
                "depends_on": [],
                "execution": "always",
                "tools": [
                    {
                        "id": "profile_selected_dataset",
                        "order": 1,
                        "tool": "profile_data",
                        "tool_origin": "registry",
                        "tool_source": "app/workflow/tools/eda/profile_data.py",
                        "selection_reason": "Profile the selected analysis data.",
                        "execution": "always",
                        "arguments": {
                            "data": "${steps.load_data_1.outputs.data}"
                        },
                        "argument_sources": {"data": "step_output"},
                        "returns": {
                            "result_variable": "profile_result",
                            "outputs": {
                                "profile": {
                                    "selector": '["profile"]',
                                    "variable": "selected_data_profile",
                                }
                            },
                        },
                    }
                ],
                "outputs": {
                    "profile": "${steps.profile_selected_data.tools.profile_selected_dataset.outputs.profile}"
                },
            }
        ]
        outputs = {
            "profile": "${steps.profile_selected_data.outputs.profile}"
        }
        document = {
            "schema_version": "1.3",
            "workflow": {
                "id": "test_analysis_workflow",
                "name": "Test analysis workflow",
                "description": "Offline orchestration test workflow.",
                "goal": payload["user_request"],
                "status": "needs_input" if needs_input else "ready",
                "execution_mode": "static",
                "input_schema": input_schema,
                "inputs": inputs,
                "input_provenance": provenance,
                "unresolved_inputs": unresolved,
                "context": {
                    "selected_datasets": selection["datasets"],
                    "analysis_context": payload["analysis_context"],
                },
                "steps": steps,
                "outputs": outputs,
            },
        }
        return document

    async def ainvoke(self, payload):
        return self.invoke(payload)


class PlanWorkflowAgent:
    def __init__(self):
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        target = payload["additional_information"].get("target_column")
        needs_input = target is None
        data_argument = {
            "source": "step_output",
            "step_id": "load_data_1",
            "output": "data",
        }
        return {
            "plan_version": "1.0",
            "workflow": {
                "id": "test_analysis_workflow",
                "name": "Test analysis workflow",
                "description": "Offline orchestration test workflow.",
                "goal": payload["user_request"],
                "status": "needs_input" if needs_input else "ready",
                "input_schema": {
                    "target_column": {
                        "type": "str",
                        "required": True,
                        "allow_llm_inference": False,
                        "validation": [],
                    }
                },
                "inputs": ({"target_column": target} if target else {}),
                "input_provenance": {
                    "target_column": {
                        "source": "user_answer" if target else None,
                        "confirmed": bool(target),
                    }
                },
                "unresolved_inputs": (
                    [
                        {
                            "name": "target_column",
                            "question": "예측 대상 컬럼명을 입력해주세요.",
                            "required_for": ["analysis"],
                        }
                    ]
                    if needs_input
                    else []
                ),
                "context": payload.get("workflow_context", {}),
                "steps": [
                    {
                        "id": "profile_selected_data",
                        "skill": "data_quality_check",
                        "depends_on": [],
                        "tools": [
                            {
                                "tool": name,
                                "selection_reason": "Profile the selected analysis data.",
                                "arguments": {"data": data_argument},
                            }
                            for name in (
                                "profile_data",
                                "compute_statistics",
                                "detect_outliers",
                            )
                        ],
                    }
                ],
                "outputs": {
                    "profile": {
                        "step_id": "profile_selected_data",
                        "tool": "profile_data",
                        "output": "profile",
                    }
                },
            },
        }

    async def ainvoke(self, payload):
        return self.invoke(payload)


class ValidationRetryWorkflowAgent(PlanWorkflowAgent):
    def invoke(self, payload):
        document = super().invoke(payload)
        if (
            payload["additional_information"].get("target_column")
            and payload.get("validation_feedback") is None
        ):
            tool = document["workflow"]["steps"][0]["tools"][0]
            tool["arguments"]["not_a_registry_input"] = {
                "source": "planner",
                "value": True,
            }
        return document

    async def ainvoke(self, payload):
        return self.invoke(payload)


class PlaceholderAgent:
    def __init__(self, message):
        self.message = message

    def invoke(self, payload):
        return {"status": "placeholder", "message": self.message}

    async def ainvoke(self, payload):
        return self.invoke(payload)


class FakeChatModel:
    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content="간단한 FAQ 답변입니다.")

    async def ainvoke(self, payload):
        return self.invoke(payload)


def settings(
    *,
    recommendation_enabled=False,
    artifacts_enabled=True,
    artifacts_root=None,
):
    if artifacts_root is None:
        artifacts_root = _TEST_ARTIFACTS.name
    environ = {
            "LLM_MODEL_NAME": "test-model",
            "MODEL_API_KEY": "test-key",
            "API_BASE_URL": "http://model.invalid/v1",
            "CHECKPOINT_DB_URI": (
                "postgresql://postgres:postgres@localhost:5432/test"
            ),
            "MAX_WORKFLOW_REVISIONS": "8",
            "WORKFLOW_RECOMMENDATION_ENABLED": (
                "true" if recommendation_enabled else "false"
            ),
            "WORKFLOW_SIMILARITY_SCORE": "0.93",
            "DEMO_ARTIFACTS_ENABLED": (
                "true" if artifacts_enabled else "false"
            ),
        }
    if artifacts_root is not None:
        environ["DEMO_ARTIFACTS_ROOT"] = str(artifacts_root)
        environ["EXECUTOR_SHARED_INPUT_ROOT"] = str(artifacts_root)
    return load_agent_settings(environ)


def message_statuses(messages):
    statuses = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                continue
        if isinstance(content, dict) and content.get("status"):
            statuses.append(content["status"])
    return statuses


def dependencies(
    *,
    workflow_recommender=None,
    workflow_agent=None,
    skill_selector_agent=None,
):
    recommendation = workflow_recommender or WorkflowRecommender()
    workflow = workflow_agent or PlanWorkflowAgent()
    return (
        AgentDependencies(
            routing_agent=RoutingAgent(),
            analysis_intent_agent=ConstantAgent(
                {"intent": "failure_prediction", "reason": "prediction request"}
            ),
            workflow_recommender=recommendation,
            workflow_agent=workflow,
            faq_agent=ConstantAgent({"answer": "faq answer"}),
            skill_selector_agent=skill_selector_agent,
        ),
        recommendation,
        workflow,
    )


def interrupt_payload(result):
    interrupts = result["__interrupt__"]
    return hitl_request_args(interrupts[0].value)


def data_selection():
    return {
        "data_count": 2,
        "datasets": [
            {
                "role": "x",
                "data_type": "nce",
                "lot_cd": "6E2",
                "process": ["ALL"],
                "query_mode": "period",
                "start_dt": "2026-05-01",
                "end_dt": "2026-05-10",
                "limit": 100000000000,
                "transform_op": "pivot",
            },
            {
                "role": "y",
                "data_type": "wt_symbol",
                "lot_cd": "6E2",
                "process": ["PT1H"],
                "query_mode": "period",
                "start_dt": "2026-08-01",
                "end_dt": "2026-08-01",
                "limit": 1000001,
                "transform_op": "wt_fail_pivot",
            },
        ],
    }


def analysis_context():
    return {
        "objective": "Explore process behavior and predict wafer failures.",
    }


def recommended_workflow_response():
    workflow = WorkflowAgent().invoke(
        {
            "user_request": "Predict wafer failures",
            "analysis_context": analysis_context(),
            "data_selection": data_selection(),
            "additional_information": {},
        }
    )
    canonicalize_registry_tool_sources(workflow)
    workflow = merge_required_data_load_steps(workflow, data_selection())
    workflow["workflow"]["id"] = "fixture_failure_prediction_v1"
    workflow["workflow"]["name"] = "Fixture failure prediction workflow"
    return {
        "recommendation_available": True,
        "recommendation": {
            "workflow_id": "fixture_failure_prediction_v1",
            "similarity_score": 0.93,
            "reason": "Fixture for the future DB recommendation branch.",
            "workflow": workflow,
        },
        "no_match_reason": None,
    }


def select_candidate(graph, config, result, origin="generated"):
    payload = interrupt_payload(result)
    if payload.get("candidate_number") != 1:
        raise AssertionError("expected workflow_candidate_selection interrupt")
    candidates = graph.get_state(config).values["workflow_candidates"]
    candidate = next(
        item for item in candidates if item["origin"] == origin
    )
    return asyncio.run(graph.ainvoke(
        Command(resume={"selected_candidate_id": candidate["id"]}),
        config=config,
    ))


class UserAgentGraphTests(unittest.TestCase):
    def test_nested_agent_does_not_inherit_outer_async_checkpointer(self):
        nested = Mock()
        nested.invoke.return_value = {"messages": []}

        JsonMessageAgentAdapter(nested).invoke({"request": "test"})

        config = nested.invoke.call_args.kwargs["config"]
        self.assertIsNone(
            config["configurable"]["__pregel_checkpointer"]
        )

    def test_workflow_candidate_description_shows_execution_structure(self):
        description = _workflow_candidate_description(
            [
                {
                    "id": "generated_1",
                    "origin": "generated",
                    "status": "needs_input",
                    "workflow": {
                        "workflow": {
                            "name": "Failure Prediction EDA",
                            "goal": "불량 예측용 EDA",
                            "status": "needs_input",
                            "execution_mode": "adaptive",
                            "unresolved_inputs": [{"name": "target_column"}],
                            "steps": [
                                {
                                    "id": "eda",
                                    "order": 1,
                                    "skill": "eda_analysis",
                                    "depends_on": ["load_data_1"],
                                    "execution": "always",
                                    "tools": [
                                        {
                                            "tool": "detect_outliers",
                                            "execution": "conditional",
                                            "condition": {
                                                "type": "runtime_decision",
                                                "decision_id": "outlier_check",
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                }
            ],
            "후보를 선택해주세요.",
        )

        self.assertIn("### 1. Failure Prediction EDA", description)
        self.assertIn("실행 모드: `adaptive`", description)
        self.assertIn("필요 입력: target_column", description)
        self.assertIn("Step 0: `eda_analysis`", description)
        self.assertIn("detect_outliers", description)
        self.assertIn("conditional", description)
        self.assertIn("decision_id: outlier_check", description)

    def test_hitl_request_uses_agent_chat_standard_envelope(self):
        request = build_hitl_request(
            action_name="data_selection",
            description="데이터를 선택해주세요.",
            args={"kind": "data_selection", "test_shortcut": "mock"},
            allowed_decisions=["approve", "edit"],
            args_schema={"type": "object"},
        )

        self.assertEqual(
            request["action_requests"][0]["name"],
            "data_selection",
        )
        self.assertEqual(
            request["action_requests"][0]["args"],
            {"kind": "data_selection", "test_shortcut": "mock"},
        )
        self.assertEqual(
            request["action_requests"][0]["description"],
            "데이터를 선택해주세요.",
        )
        self.assertEqual(
            request["review_configs"],
            [
                {
                    "action_name": "data_selection",
                    "allowed_decisions": ["approve", "edit"],
                    "args_schema": {"type": "object"},
                }
            ],
        )

    def test_hitl_response_unwraps_agent_chat_respond_message(self):
        self.assertEqual(
            unwrap_hitl_response(
                {
                    "decisions": [
                        {"type": "respond", "message": "mock"}
                    ]
                }
            ),
            "mock",
        )
        self.assertEqual(
            unwrap_hitl_response(
                {"decisions": [{"type": "approve"}]},
                default_args={"data_count": 2},
            ),
            {"data_count": 2},
        )
        self.assertEqual(
            unwrap_hitl_response(
                {
                    "decisions": [
                        {
                            "type": "edit",
                            "edited_action": {
                                "name": "data_selection",
                                "args": {"data_count": 3},
                            },
                        }
                    ]
                },
                default_args={"data_count": 2},
            ),
            {"data_count": 3},
        )
        self.assertEqual(
            unwrap_hitl_response(
                {
                    "decisions": [
                        {"type": "reject", "message": "후보를 다시 만들어주세요."}
                    ]
                },
                default_args={"candidate_number": 1},
            ),
            {"approved": False, "feedback": "후보를 다시 만들어주세요."},
        )
        self.assertEqual(
            unwrap_hitl_response(
                {
                    "decisions": [
                        {
                            "type": "respond",
                            "message": '{"objective":"불량 예측"}',
                        }
                    ]
                }
            ),
            {"objective": "불량 예측"},
        )
        self.assertEqual(
            unwrap_hitl_response(
                [{"type": "response", "args": "mock"}]
            ),
            "mock",
        )


    def test_receive_request_accepts_agent_chat_message_input(self):
        result = receive_request(
            {
                "messages": [
                    {"role": "user", "content": "FAQ: 데이터 드리프트란?"}
                ]
            },
            {"configurable": {"thread_id": "agent-chat-thread"}},
        )

        self.assertEqual(result["user_request"], "FAQ: 데이터 드리프트란?")
        self.assertEqual(result["session_id"], "agent-chat-thread")
        self.assertEqual(result["thread_id"], "agent-chat-thread")
        self.assertEqual(result["user_id"], "mock-user-001")
        self.assertEqual(result["project_id"], "mock-project-001")
        self.assertEqual(result["messages"], [])

    def test_message_reducer_assigns_unique_agent_chat_ids(self):
        messages = append_messages_with_ids(
            [{"id": "existing", "role": "user", "content": "질문"}],
            [
                {"role": "assistant", "content": "진행 1"},
                {"role": "assistant", "content": "진행 2"},
            ],
        )

        self.assertEqual(messages[0]["id"], "existing")
        self.assertEqual(messages[0]["type"], "human")
        self.assertTrue(messages[1]["id"])
        self.assertTrue(messages[2]["id"])
        self.assertNotEqual(messages[1]["id"], messages[2]["id"])
        self.assertEqual(messages[1]["type"], "ai")
        self.assertEqual(messages[2]["type"], "ai")

    def test_graph_accepts_agent_chat_message_input(self):
        deps, _, _ = dependencies()
        graph = compiled_in_memory_graph(deps, settings())
        config = {"configurable": {"thread_id": "agent-chat-graph"}}

        result = asyncio.run(graph.ainvoke(
            {
                "messages": [
                    {"role": "user", "content": "FAQ: 데이터 드리프트란?"}
                ]
            },
            config=config,
        ))

        self.assertEqual(result["user_request"], "FAQ: 데이터 드리프트란?")
        self.assertEqual(result["session_id"], "agent-chat-graph")
        self.assertEqual(result["service_response"]["service"], "faq")
        self.assertTrue(
            any(
                message.get("role") == "assistant"
                for message in result["messages"]
            )
        )
        self.assertTrue(
            all(
                isinstance(message.get("content"), str)
                for message in result["messages"]
            )
        )

    def test_registry_tool_source_is_canonicalized(self):
        document = {
            "workflow": {
                "steps": [
                    {
                        "tools": [
                            {
                                "tool": "merge_data",
                                "tool_source": "preprocessing/merge_data.py",
                            }
                        ]
                    }
                ]
            }
        }

        canonicalize_registry_tool_sources(document)

        self.assertEqual(
            document["workflow"]["steps"][0]["tools"][0]["tool_source"],
            "app/workflow/tools/preprocessing/merge_data.py",
        )

    def test_label_only_agent_builds_typed_output_without_json(self):
        model = FakeChatModel()
        model.invoke = lambda messages: AIMessage(content="analysis")
        agent = LabelOnlyLLMAgent(
            model=model,
            system_prompt="Classify the request.",
            output_type=RoutingOutput,
            label_field="route",
            allowed_labels=("analysis", "faq"),
        )

        result = agent.invoke({"user_request": "불량 예측을 하고 싶어"})

        self.assertEqual(result.route, "analysis")
        self.assertIn("analysis", result.reason)

    def test_label_only_agent_rejects_unexpected_text(self):
        model = FakeChatModel()
        model.invoke = lambda messages: AIMessage(
            content="The answer is analysis."
        )
        agent = LabelOnlyLLMAgent(
            model=model,
            system_prompt="Classify the request.",
            output_type=RoutingOutput,
            label_field="route",
            allowed_labels=("analysis", "faq"),
        )

        with self.assertRaisesRegex(ValueError, "Unexpected LLM label"):
            agent.invoke({"user_request": "불량 예측을 하고 싶어"})

    def test_data_load_merge_canonicalizes_local_tool_output_references(self):
        document = WorkflowAgent().invoke(
            {
                "user_request": "EDA를 수행해줘",
                "analysis_context": analysis_context(),
                "data_selection": data_selection(),
                "additional_information": {"target_column": "fail"},
            }
        )
        document["workflow"]["steps"][0]["outputs"]["profile"] = (
            "${tools.profile_selected_dataset.outputs.profile}"
        )
        profile_tool = document["workflow"]["steps"][0]["tools"][0]
        profile_tool["arguments"]["target_column"] = "fail"
        profile_tool["argument_sources"]["target_column"] = "workflow_input"

        merged = merge_required_data_load_steps(document, data_selection())
        steps = merged["workflow"]["steps"]

        self.assertEqual([step["order"] for step in steps], [1, 2, 3])
        self.assertEqual(steps[2]["depends_on"], ["load_data_1", "load_data_2"])
        self.assertEqual(
            steps[2]["outputs"]["profile"],
            "${steps.profile_selected_data.tools.profile_selected_dataset.outputs.profile}",
        )
        self.assertEqual(
            steps[2]["tools"][0]["arguments"]["target_column"],
            "${workflow.inputs.target_column}",
        )

    def test_workflow_generator_output_rejects_adaptive_without_conditional_tool(self):
        document = WorkflowAgent().invoke(
            {
                "user_request": "EDA를 수행해줘",
                "analysis_context": analysis_context(),
                "data_selection": data_selection(),
                "additional_information": {"target_column": "fail"},
            }
        )
        document["workflow"]["execution_mode"] = "adaptive"

        with self.assertRaisesRegex(ValueError, "adaptive Workflow"):
            WorkflowGeneratorOutput.model_validate(document)

    def test_cli_workflow_summary_lists_tools_under_skills(self):
        payload = {
            "workflow": {
                "workflow": {
                    "name": "EDA Workflow",
                    "goal": "EDA 수행",
                    "steps": [
                        {
                            "order": 1,
                            "id": "load_data_1",
                            "skill": "data_load",
                            "tools": [{"tool": "data_load"}],
                        },
                        {
                            "order": 2,
                            "id": "eda",
                            "skill": "exploratory_analysis",
                            "tools": [
                                {"tool": "histogram_eda"},
                                {"tool": "correlation_analysis"},
                            ],
                        },
                    ],
                }
            }
        }
        output = io.StringIO()

        with redirect_stdout(output):
            _print_workflow_summary(payload)

        rendered = output.getvalue()
        self.assertIn("1. data_load (load_data_1)", rendered)
        self.assertIn("- data_load", rendered)
        self.assertIn("2. exploratory_analysis (eda)", rendered)
        self.assertIn("- histogram_eda", rendered)
        self.assertIn("- correlation_analysis", rendered)

    def test_hitl_coerces_list_answers_by_input_schema(self):
        result = _coerce_answers_by_input_schema(
            {"join_columns": "alias_lot_id, wf_id, x, y"},
            {"join_columns": {"type": "list[str]"}},
        )

        self.assertEqual(
            result,
            {"join_columns": ["alias_lot_id", "wf_id", "x", "y"]},
        )

    def test_additional_information_confirms_unresolved_workflow_input(self):
        document = {
            "workflow": {
                "status": "needs_input",
                "input_schema": {
                    "join_columns": {
                        "type": "list[str]",
                        "required": True,
                        "allow_llm_inference": False,
                        "validation": [],
                    }
                },
                "inputs": {"join_columns": None},
                "input_provenance": {
                    "join_columns": {"source": None, "confirmed": False}
                },
                "unresolved_inputs": [
                    {
                        "name": "join_columns",
                        "question": "join columns",
                        "required_for": ["merge_data"],
                    }
                ],
            }
        }

        result = _apply_confirmed_additional_information(
            document,
            {"join_columns": ["alias_lot_id", "wf_id", "x", "y"]},
        )

        workflow = result["workflow"]
        self.assertEqual(workflow["status"], "ready")
        self.assertEqual(workflow["unresolved_inputs"], [])
        self.assertEqual(
            workflow["inputs"]["join_columns"],
            ["alias_lot_id", "wf_id", "x", "y"],
        )
        self.assertEqual(
            workflow["input_provenance"]["join_columns"],
            {"source": "user_answer", "confirmed": True},
        )

    def test_planner_cannot_confirm_join_columns_for_the_user(self):
        document = {
            "workflow": {
                "status": "ready",
                "input_schema": {
                    "join_columns": {
                        "type": "list[str]",
                        "required": True,
                        "allow_llm_inference": False,
                        "validation": [],
                    }
                },
                "inputs": {"join_columns": ["lot_cd"]},
                "input_provenance": {
                    "join_columns": {"source": "user_query", "confirmed": True}
                },
                "unresolved_inputs": [],
            }
        }

        result = _require_human_confirmation_for_inputs(document, {})
        workflow = result["workflow"]

        self.assertEqual(workflow["status"], "needs_input")
        self.assertIsNone(workflow["inputs"]["join_columns"])
        self.assertEqual(
            workflow["input_provenance"]["join_columns"],
            {"source": None, "confirmed": False},
        )
        self.assertEqual(
            [item["name"] for item in workflow["unresolved_inputs"]],
            ["join_columns"],
        )

    def test_generate_report_matches_execution_events_to_steps(self):
        report_agent = ReportAgent()
        artifact_calls = []

        def submit_artifact(_settings, execution_id, payload):
            artifact_calls.append((execution_id, payload))
            return {"status_code": 201, "body": {"name": "final-report.md"}}
        deps = AgentDependencies(
            routing_agent=RoutingAgent(),
            analysis_intent_agent=ConstantAgent(
                {"intent": "failure_prediction", "reason": "prediction request"}
            ),
            workflow_recommender=WorkflowRecommender(),
            workflow_agent=WorkflowAgent(),
            faq_agent=ConstantAgent({"answer": "faq"}),
            report_agent=report_agent,
        )
        test_config = settings()

        result = asyncio.run(make_generate_report(
            deps, test_config, submit_artifact=submit_artifact
        )(
            {
                "user_id": "user-1",
                "project_id": "project-1",
                "session_id": "session-1",
                "task_id": "task-1",
                "user_request": "불량예측할래",
                "analysis_intent": {"intent": "failure_prediction"},
                "workflow": {
                    "workflow": {
                        "id": "wf-1",
                        "name": "Failure prediction",
                    }
                },
                "execution_id": "executor-exec-1",
                "execution_steps": [
                    {
                        "sequence": 0,
                        "payload": {
                            "type": ".py",
                            "path": "cells/001_load_x.py",
                        },
                        "lineage": {
                            "skill": "data_load",
                            "step_id": "load_data",
                            "tool": "data_load",
                            "tool_id": "load_x",
                        },
                    }
                ],
                "execution_events": [
                    {
                        "event_id": "event-1",
                        "payload": json.dumps(
                            {
                                "execution_id": "executor-exec-1",
                                "step_id": "load_data",
                                "status": "SUCCESS",
                                "message": "Loaded 100 rows.",
                            }
                        ),
                    }
                ],
                "artifact_files": {},
            }
        ))

        self.assertEqual(result["report_status"], "generated")
        self.assertEqual(result["step_results"][0]["status"], "SUCCESS")
        self.assertEqual(result["step_results"][0]["step_id"], "load_data")
        self.assertEqual(
            report_agent.calls[0]["step_results"][0]["message"],
            "Loaded 100 rows.",
        )
        self.assertEqual(
            result["final_response"]["status"], "report_generated"
        )
        self.assertIn("# 분석 리포트", result["analysis_report"]["content"])
        self.assertTrue(
            Path(result["artifact_files"]["analysis_report"]).exists()
        )
        self.assertEqual(artifact_calls[0][0], "executor-exec-1")
        self.assertEqual(artifact_calls[0][1]["type"], "REPORT")
        self.assertEqual(artifact_calls[0][1]["source"]["type"], "INLINE")
        self.assertTrue(artifact_calls[0][1]["append_to_notebook"])

    def test_cli_hides_internal_classification_messages_before_interrupt(self):
        from app.agents.orchestration.mock_llm import quality_plan
        deps, _, _ = dependencies(workflow_agent=ConstantAgent(quality_plan()))
        graph = compiled_in_memory_graph(deps, settings())
        context = build_local_mock_request_context(
            session_id="cli-stream-test"
        )
        config = {"configurable": {"thread_id": context["thread_id"]}}
        output = io.StringIO()

        with redirect_stdout(output):
            result, displayed = asyncio.run(_stream_graph(
                graph,
                {**context, "user_request": "불량 예측 분석"},
                config=config,
                displayed_messages=0,
            ))

        self.assertTrue(result.get("__interrupt__"))
        self.assertEqual(displayed, len(result["messages"]))
        self.assertNotIn("[routing_agent]", output.getvalue())
        self.assertNotIn("[analysis_intent]", output.getvalue())


    @patch(
        "app.agents.workflow_generator.create_workflow_generator_agent.create_agent"
    )
    def test_workflow_agent_uses_structured_output_without_resource_tool(
        self, create_agent_mock
    ):
        expected_agent = object()
        create_agent_mock.return_value = expected_agent

        actual_agent = create_workflow_generator_agent(object())

        system_prompt = create_agent_mock.call_args.kwargs["system_prompt"]
        tools = create_agent_mock.call_args.kwargs["tools"]
        self.assertIs(actual_agent, expected_agent)
        self.assertIn("workflow_resources", system_prompt)
        self.assertIn("JSON Schema", system_prompt)
        self.assertNotIn(
            "response_format",
            create_agent_mock.call_args.kwargs,
        )
        self.assertEqual(tools, [])

    @patch(
        "app.agents.workflow_generator.create_workflow_generator_agent.create_agent"
    )
    def test_provider_workflow_agent_binds_json_schema(
        self, create_agent_mock
    ):
        model = Mock()
        bound_model = object()
        model.bind.return_value = bound_model

        create_workflow_generator_agent(
            model,
            structured_output_mode="provider_json_schema",
        )

        response_format = model.bind.call_args.kwargs["response_format"]
        agent_kwargs = create_agent_mock.call_args.kwargs
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertIs(agent_kwargs["model"], bound_model)
        self.assertNotIn("response_format", agent_kwargs)
        self.assertIn("JSON Schema", agent_kwargs["system_prompt"])
        self.assertEqual(agent_kwargs["tools"], [])

    def test_invoke_typed_parses_last_ai_message_content(self):
        agent = ConstantAgent(
            {
                "messages": [
                    AIMessage(
                        content=json.dumps(
                            {
                                "intent": "failure_prediction",
                                "reason": "prediction request",
                            }
                        )
                    )
                ]
            }
        )

        result = asyncio.run(make_classify_analysis_intent(
            AgentDependencies(
                routing_agent=RoutingAgent(),
                analysis_intent_agent=agent,
                workflow_recommender=WorkflowRecommender(),
                workflow_agent=WorkflowAgent(),
                faq_agent=ConstantAgent({"answer": "faq"}),
            )
        )({"user_request": "failure prediction"}))

        self.assertEqual(
            result["analysis_intent"]["intent"], "failure_prediction"
        )

    def test_invoke_typed_unwraps_single_json_code_fence(self):
        agent = ConstantAgent(
            {
                "messages": [
                    AIMessage(
                        content=(
                            "```json\n"
                            '{"skill_names":["data_quality_check"]}\n'
                            "```"
                        )
                    )
                ]
            }
        )

        result = invoke_typed(agent, {}, SkillSelectionOutput)

        self.assertEqual(result.skill_names, ["data_quality_check"])

    def test_workflow_catalog_and_skill_documents_are_loaded_in_batches(self):
        catalog = load_workflow_catalog_context()
        documents = read_skill_documents.invoke(
            {
                "skill_names": [
                    "data_load",
                    "data_quality_check.md",
                ]
            }
        )

        self.assertIn("index_type: skill_index", catalog)
        self.assertNotIn("registry_type: tool_registry", catalog)
        self.assertEqual(
            list(documents["skills"]),
            ["data_load", "data_quality_check"],
        )
        self.assertEqual(
            documents["skills"]["data_load"]["source"],
            "data_io/data_load.md",
        )
        self.assertIn(
            "name: data_load",
            documents["skills"]["data_load"]["content"],
        )
        self.assertEqual(
            documents["skills"]["data_quality_check"]["source"],
            "eda/data_quality_check.md",
        )
        self.assertIn("extract_data", documents["tools"])
        self.assertIn("transform_data", documents["tools"])
        self.assertNotIn("transform_nce", documents["tools"])
        self.assertNotIn("transform_wt", documents["tools"])
        self.assertIn("profile_data", documents["tools"])
        self.assertIn("compute_statistics", documents["tools"])
        self.assertIn("detect_outliers", documents["tools"])
        self.assertNotIn("boxplot_eda", documents["tools"])

    def test_skill_selection_requires_unique_known_index_names(self):
        selection = SkillSelectionOutput(
            skill_names=["data_quality_check", "predictive_modeling"]
        )

        self.assertEqual(
            validate_skill_names(selection.skill_names),
            ["data_quality_check", "predictive_modeling"],
        )
        with self.assertRaisesRegex(ValueError, "Skill Index"):
            validate_skill_names(["invented_skill"])

    def test_prompt_json_agent_retries_pydantic_validation(self):
        model = Mock()
        model.invoke.side_effect = [
            AIMessage(content="not json"),
            AIMessage(content='{"skill_names":["data_quality_check"]}'),
        ]
        agent = StructuredLLMAgent(
            model=model,
            system_prompt="Select Skills.",
            output_type=SkillSelectionOutput,
            method="prompt_json",
        )

        result = agent.invoke({"user_request": "profile data"})

        self.assertEqual(result.skill_names, ["data_quality_check"])
        self.assertEqual(model.invoke.call_count, 2)
        retry_messages = model.invoke.call_args.args[0]
        self.assertIn("Validation error", retry_messages[-1].content)

    def test_prompt_json_agent_accepts_single_json_code_fence_without_retry(self):
        model = Mock()
        model.invoke.return_value = AIMessage(
            content=(
                "```json\n"
                '{"skill_names":["data_quality_check"]}\n'
                "```"
            )
        )
        agent = StructuredLLMAgent(
            model=model,
            system_prompt="Select Skills.",
            output_type=SkillSelectionOutput,
            method="prompt_json",
        )

        result = agent.invoke({"user_request": "profile data"})

        self.assertEqual(result.skill_names, ["data_quality_check"])
        self.assertEqual(model.invoke.call_count, 1)

    def test_workflow_input_defaults_to_required_when_llm_omits_field(self):
        definition = InputDefinition.model_validate(
            {
                "type": "string",
                "allow_llm_inference": False,
                "validation": [],
            }
        )

        self.assertTrue(definition.required)

    def test_analysis_intent_respects_enabled_configuration(self):
        deps, _, _ = dependencies()
        disabled_intent_agent = ConstantAgent(
            {"intent": "root_cause", "reason": "ambiguous request"}
        )
        deps = AgentDependencies(
            routing_agent=deps.routing_agent,
            analysis_intent_agent=disabled_intent_agent,
            workflow_recommender=deps.workflow_recommender,
            workflow_agent=deps.workflow_agent,
            faq_agent=deps.faq_agent,
        )

        result = asyncio.run(make_classify_analysis_intent(deps)(
            {"user_request": "불량 원인을 분석해줘"}
        ))

        self.assertIn(
            result["analysis_intent"]["intent"], ENABLED_ANALYSIS_INTENTS
        )
        expected_call_count = 0 if len(ENABLED_ANALYSIS_INTENTS) == 1 else 1
        self.assertEqual(len(disabled_intent_agent.calls), expected_call_count)

    def test_simple_llm_agent_returns_plain_faq_answer(self):
        model = FakeChatModel()
        agent = SimpleLLMAgent(
            model=model,
            system_prompt="FAQ에 답변하세요.",
        )

        result = agent.invoke({"user_request": "데이터 드리프트가 무엇인가요?"})

        self.assertEqual(result, {"answer": "간단한 FAQ 답변입니다."})
        self.assertEqual(model.calls[0][0].content, "FAQ에 답변하세요.")
        self.assertEqual(
            model.calls[0][1].content,
            "데이터 드리프트가 무엇인가요?",
        )

    def test_mock_context_generates_a_new_request_per_call(self):
        first = build_local_mock_request_context(session_id="same-session")
        second = build_local_mock_request_context(session_id="same-session")

        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["thread_id"], "same-session")
        self.assertEqual(second["thread_id"], "same-session")
        self.assertNotIn("run_id", first)
        self.assertNotIn("run_id", second)

    def test_request_context_uses_session_as_thread_id(self):
        from app.agents.orchestration.mock_llm import quality_plan
        deps, _, _ = dependencies(workflow_agent=ConstantAgent(quality_plan()))
        graph = compiled_in_memory_graph(deps, settings())
        context = build_local_mock_request_context(session_id="session-a")
        config = {"configurable": {"thread_id": context["thread_id"]}}

        result = asyncio.run(graph.ainvoke(
            {
                **context,
                "request_id": "request-session-thread",
                "user_request": "Predict wafer failures",
            },
            config=config,
        ))

        self.assertEqual(result["thread_id"], "session-a")
        self.assertIn("task_id", result)
        self.assertNotEqual(result["task_id"], result["thread_id"])
        self.assertNotIn("run_id", result)

    def test_each_analysis_gets_a_new_task_id(self):
        first = ensure_analysis_task({"thread_id": "session-a"})
        second = ensure_analysis_task(
            {"thread_id": "session-a", "task_id": first["task_id"]}
        )

        self.assertNotEqual(first["task_id"], "session-a")
        self.assertNotEqual(first["task_id"], second["task_id"])

    def test_request_context_uses_agent_chat_fallbacks(self):
        from app.agents.orchestration.mock_llm import quality_plan
        deps, _, _ = dependencies(workflow_agent=ConstantAgent(quality_plan()))
        graph = compiled_in_memory_graph(deps, settings())
        config = {"configurable": {"thread_id": "missing-context"}}

        result = asyncio.run(graph.ainvoke(
            {
                "request_id": "request-missing-context",
                "user_request": "Predict wafer failures",
            },
            config=config,
        ))

        self.assertEqual(result["user_id"], "mock-user-001")
        self.assertEqual(result["project_id"], "mock-project-001")
        self.assertEqual(result["session_id"], "missing-context")


    def test_import_compile_and_mermaid(self):
        deps, _, _ = dependencies()
        graph = compiled_in_memory_graph(deps, settings())
        node_names = set(graph.get_graph().nodes)
        expected = {
            "announce_workflow_search",
            "generate_workflow",
            "select_workflow_candidate",
            "await_next_user_request",
            "save_approved_workflow",
            "cancel_request",
        }
        self.assertTrue(expected.issubset(node_names))
        self.assertNotIn("reset_analysis_state", node_names)
        self.assertNotIn("file_lookup", node_names)
        self.assertNotIn("collect_rejection_request", node_names)
        mermaid = graph_to_mermaid(graph)
        self.assertNotIn("review_recommendation", mermaid)
        self.assertIn("announce_workflow_search", mermaid)
        self.assertNotIn("custom_analysis", mermaid)
        standalone_graph = build_visualization_graph()
        self.assertIn(
            "collect_missing_information",
            graph_to_mermaid(standalone_graph),
        )

    def test_faq_and_unsupported_file_request_return_to_conversation_hub(self):
        deps, _, _ = dependencies()
        graph = compiled_in_memory_graph(deps, settings())
        context = build_local_mock_request_context(
            session_id="thread-services"
        )
        config = {"configurable": {"thread_id": context["thread_id"]}}

        result = asyncio.run(graph.ainvoke(
            {
                **context,
                "request_id": "request-services",
                "user_request": "FAQ: what is data drift?",
            },
            config=config,
        ))
        self.assertEqual(result["service_response"]["service"], "faq")
        self.assertEqual(
            result["service_response"]["response"],
            {"answer": "faq answer"},
        )
        self.assertEqual(
            interrupt_payload(result)["kind"], "next_user_request"
        )

        result = asyncio.run(graph.ainvoke(
            Command(
                resume={
                    "action": "continue",
                    "user_request": "Find this file",
                }
            ),
            config=config,
        ))
        self.assertEqual(result["service_response"]["service"], "faq")
        self.assertEqual(
            interrupt_payload(result)["kind"], "next_user_request"
        )

        final = asyncio.run(graph.ainvoke(
            Command(resume={"action": "cancel"}),
            config=config,
        ))
        self.assertEqual(final["final_response"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
