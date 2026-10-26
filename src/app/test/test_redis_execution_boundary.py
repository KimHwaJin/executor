"""Tests for adapting durable Worker events to Agent execution state."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from langgraph.checkpoint.memory import InMemorySaver

from agent_config import load_agent_settings
from app.agent_worker.graph_provider import build_agent_graph
from app.graphs.nodes.redis_execution_events import (
    apply_redis_execution_event,
    collect_final_execution_event,
)
from app.graphs.routers.orchestration_router import route_redis_execution_event


class RedisExecutionBoundaryTests(unittest.TestCase):
    def test_worker_graph_provider_builds_service_graph_in_redis_mode(self):
        settings = load_agent_settings()
        bindings = MagicMock()
        checkpointer = InMemorySaver()
        with (
            patch(
                "app.agent_worker.graph_provider.load_agent_settings",
                return_value=settings,
            ),
            patch(
                "app.agent_worker.graph_provider.create_llm_dependencies",
                return_value=MagicMock(),
            ),
        ):
            graph = build_agent_graph(
                bindings=bindings,
                checkpointer=checkpointer,
            )

        self.assertIs(graph.checkpointer, checkpointer)

    def test_operation_event_is_normalized_for_existing_result_nodes(self):
        state = {
            "session_id": "session-1",
            "task_id": "task-1",
            "execution_id": "execution-1",
            "execution_mode": "MULTI",
            "executor_operation_number": 2,
            "executor_state_version": 4,
            "executor_wait_phase": "operation_completed",
            "execution_events": [],
            "ew_pending": {
                "execution_id": "execution-1",
                "event": {
                    "event_id": "event-1",
                    "event_type": "execution.operation_completed",
                    "payload": {
                        "status": "WAITING_FOR_OPERATION",
                        "continuation": {
                            "allowed": True,
                            "expected_version": 5,
                        },
                    },
                },
            },
        }

        update = apply_redis_execution_event(state)

        self.assertEqual(update["execution_event"]["state_version"], 5)
        self.assertEqual(update["execution_event"]["operation_number"], 2)
        self.assertEqual(len(update["execution_events"]), 1)
        self.assertEqual(
            route_redis_execution_event({**state, **update}),
            "collect_adaptive_execution_results",
        )

    def test_final_multi_event_routes_to_final_collector(self):
        state = {
            "task_id": "task-1",
            "execution_id": "execution-1",
            "execution_mode": "MULTI",
            "executor_wait_phase": "execution_completed",
        }
        self.assertEqual(
            route_redis_execution_event(state),
            "collect_final_execution_event",
        )
        update = asyncio.run(collect_final_execution_event(
            {
                **state,
                "execution_event": {"status": "SUCCEEDED", "state_version": 8},
            }
        ))
        self.assertEqual(update["final_response"]["execution_id"], "execution-1")
        self.assertEqual(update["messages"][0]["content"], "분석 실행이 완료되었습니다.")
        self.assertIsNone(update["execution_event"])

    def test_failed_terminal_event_produces_visible_chat_message(self):
        update = asyncio.run(collect_final_execution_event(
            {
                "task_id": "task-1",
                "execution_id": "execution-1",
                "executor_result_history": [
                    {
                        "tool_id": "cleaning.select_features",
                        "result": {
                            "status": "FAILED",
                            "error_message": "target column is missing",
                        },
                    }
                ],
                "execution_event": {
                    "status": "CANCELLED",
                    "state_version": 9,
                    "response": {},
                },
            }
        ))

        self.assertEqual(update["execution_status"], "CANCELLED")
        self.assertIn("분석 단계 오류로 실행이 중단되었습니다.", update["messages"][0]["content"])
        self.assertIn("target column is missing", update["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
