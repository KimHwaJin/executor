"""Supported routing labels and context restrictions."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.graphs.nodes.routing import ALLOWED_ROUTES, make_route_user_request
from app.schemas.agents.orchestration_schema import RoutingOutput


class RoutingContractTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_matches_supported_routes(self):
        labels = set(RoutingOutput.model_json_schema()["properties"]["route"]["enum"])
        self.assertEqual(labels, {"analysis", "analysis_question", "faq", "revise_workflow", "cancel"})
        self.assertEqual(ALLOWED_ROUTES["workflow_rejected"], labels)
        self.assertEqual(ALLOWED_ROUTES["main"], labels - {"revise_workflow"})
        for removed in ("file_lookup", "reselect_data"):
            with self.subTest(route=removed), self.assertRaises(ValidationError):
                RoutingOutput(route=removed, reason="unsupported")

    async def test_revision_requires_rejection_context(self):
        deps = SimpleNamespace(routing_agent=SimpleNamespace(ainvoke=AsyncMock(
            return_value={"route": "revise_workflow", "reason": "change dataset"}
        )))
        node = make_route_user_request(deps)
        state = {"user_request": "데이터를 바꿔줘", "routing_context": "main"}
        with self.assertRaises(ValueError):
            await node(state)
        result = await node({**state, "routing_context": "workflow_rejected"})
        self.assertEqual(result["routing_result"]["route"], "revise_workflow")
