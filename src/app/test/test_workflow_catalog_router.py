"""Offline HTTP contract tests for the local catalog router.

Run from the repository root:
    python -m unittest discover -s src/app/test -p test_workflow_catalog_router.py -v

The service mock is autospecced against WorkflowCatalogService: invalid method
names/signatures fail. This tests the HTTP boundary, not SQL or embedding servers.
The same unittest tests can also be collected by pytest.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import create_autospec, patch
from uuid import UUID

# Prefer the src package over the deployment entrypoint app.py at repository root.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import jsonschema
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import workflow_catalog as api
from app.services.workflow_catalog_service import (
    WorkflowCatalogService, WorkflowEmbeddingError, WorkflowEmbeddingTimeoutError,
    WorkflowIdempotencyConflictError, WorkflowNotFoundError,
    WorkflowRevisionConflictError, WorkflowValidationError, WorkflowVersionNotFoundError,
)


class WorkflowCatalogRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = yaml.safe_load(
            (ROOT / "docs/workflow_catalog_api/openapi.yaml").read_text(encoding="utf-8"))
        examples = cls.contract["paths"]["/workflows"]["post"]["requestBody"]["content"]["application/json"]["examples"]
        cls.register_example = next(iter(examples.values()))["value"]

    def setUp(self):
        self.request = deepcopy(self.register_example)
        self.user = UUID(self.request["created_by"])
        self.workflow_id = UUID("12345678-1234-4234-8234-123456789abc")
        self.version_id = UUID("22345678-1234-4234-8234-123456789abc")
        self.version_user = UUID("32345678-1234-4234-8234-123456789abc")
        now = datetime(2026, 10, 22, 10, tzinfo=timezone(timedelta(hours=9)))
        self.row = dict(
            workflow_id=self.workflow_id, version_id=self.version_id, revision=1,
            current_revision=2, recommend_yn="Y", created_by=self.user, updated_by=self.user,
            created_at=now, updated_at=now, version_created_by=self.version_user,
            version_created_at=now, workflow=self.request["workflow"],
            registration_type=self.request["registration_type"],
            project_id=None, session_id=None, task_id=None, search_text=None,
            deleted_at=None, internal_secret="DO_NOT_EXPOSE",
        )
        self.service = create_autospec(WorkflowCatalogService, instance=True, spec_set=True)
        self.service.register.return_value = self.row
        self.service.update.return_value = self.row
        self.service.get.return_value = self.row
        self.service.set_recommendation.return_value = self.row
        self.service.delete.return_value = None
        self.service.list.return_value = dict(items=[], page=1, page_size=20, total_count=0)
        self.service.search.return_value = dict(
            items=[], returned_count=0, limit=3, threshold=None, embedding_model="bge-m3")
        self.service_patch = patch.object(api, "_catalog_service", self.service)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.app = FastAPI()
        self.app.include_router(api.router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def check(self, response, status, schema=None):
        self.assertEqual(response.status_code, status, response.text)
        self.assertNotIn("DO_NOT_EXPOSE", response.text)
        if schema:
            jsonschema.Draft202012Validator(
                {"$ref": "#/components/schemas/" + schema,
                 "components": self.contract["components"]},
                format_checker=jsonschema.FormatChecker(),
            ).validate(response.json())
        return response

    def test_register_passes_document_sources_and_request_key(self):
        self.request.update(project_id=str(self.version_id), session_id=None, task_id=str(self.workflow_id))
        response = self.check(self.client.post("/workflows", json=self.request,
            headers={"Idempotency-Key": "register-key"}), 201, "WriteResponse")
        self.service.register.assert_awaited_once_with(
            workflow=self.request["workflow"], registration_type=self.request["registration_type"],
            created_by=self.user, project_id=self.version_id, session_id=None,
            task_id=self.workflow_id, idempotency_key="register-key")
        self.assertEqual(response.json()["workflow_id"], str(self.workflow_id))

    def test_register_defaults_and_omitted_tool_arguments_are_preserved(self):
        request = {k: self.request[k] for k in ("workflow", "registration_type", "created_by")}
        request["workflow"]["workflow"]["steps"][0]["tools"][0].pop("arguments", None)
        self.check(self.client.post("/workflows", json=request), 201, "WriteResponse")
        args = self.service.register.call_args.kwargs
        self.assertEqual(args["workflow"], request["workflow"])
        for key in ("project_id", "session_id", "task_id", "idempotency_key"):
            self.assertIsNone(args[key])

    def test_update_forwards_expected_revision_and_updater(self):
        request = {k: v for k, v in self.request.items() if k != "created_by"}
        request.update(expected_revision=1, updated_by=str(self.version_user))
        response = self.check(self.client.put(f"/workflows/{self.workflow_id}", json=request,
            headers={"Idempotency-Key": "update-key"}), 200, "WriteResponse")
        args = self.service.update.call_args
        self.assertEqual(args.args, (self.workflow_id,))
        self.assertEqual(args.kwargs["updated_by"], self.version_user)
        self.assertEqual(args.kwargs["expected_revision"], 1)
        self.assertEqual(args.kwargs["workflow"], request["workflow"])
        self.assertEqual(args.kwargs["idempotency_key"], "update-key")
        self.assertEqual(response.json()["recommend_yn"], "Y")

    def test_detail_separates_catalog_and_historical_version(self):
        response = self.check(self.client.get(f"/workflows/{self.workflow_id}?revision=1"), 200, "DetailResponse")
        data = response.json()
        self.assertEqual(data["current_revision"], 2)
        self.assertEqual(data["version"]["revision"], 1)
        self.assertEqual(data["created_by"], str(self.user))
        self.assertEqual(data["version"]["created_by"], str(self.version_user))
        self.assertEqual(data["version"]["created_at"], "2026-10-22T01:00:00Z")
        self.assertEqual(data["updated_at"], "2026-10-22T01:00:00Z")
        self.assertIsNone(data["version"]["search_text"])
        self.service.get.assert_awaited_once_with(self.workflow_id, revision=1)
        self.check(self.client.get(f"/workflows/{self.workflow_id}"), 200, "DetailResponse")
        self.assertIsNone(self.service.get.call_args.kwargs["revision"])

    def test_list_filters_pagination_and_excludes_internal_fields(self):
        fields = ("workflow_id", "version_id", "current_revision", "recommend_yn", "created_by",
                  "updated_by", "created_at", "updated_at", "registration_type")
        item = {k: self.row[k] for k in fields}
        definition = self.request["workflow"]["workflow"]
        item.update({k: definition[k] for k in ("name", "user_request", "execution_mode", "allow_immediate_execution")})
        item.update(workflow=definition, search_text="DO_NOT_EXPOSE", embedding=[1.0])
        self.service.list.return_value = dict(items=[item], page=2, page_size=10, total_count=11)
        params = dict(q="%test_", recommend_yn="Y", execution_mode="static", registration_type="USER",
                      created_by=str(self.user), page=2, page_size=10, sort_by="name", sort_order="asc")
        self.check(self.client.get("/workflows", params=params), 200, "ListResponse")
        self.service.list.assert_awaited_once_with(**{**params, "created_by": self.user})

    def test_list_defaults_and_empty_page_preserve_total(self):
        self.service.list.return_value = dict(items=[], page=99, page_size=20, total_count=3)
        result = self.check(self.client.get("/workflows?page=99"), 200, "ListResponse").json()
        self.assertEqual(result["total_count"], 3)
        self.service.list.assert_awaited_once_with(q=None, recommend_yn=None, execution_mode=None,
            registration_type=None, created_by=None, page=99, page_size=20, sort_by="updated_at", sort_order="desc")

    def test_search_defaults_empty_results_and_static_path(self):
        self.check(self.client.post("/workflows/search", json={"query": "test"}), 200, "SearchResponse")
        self.service.search.assert_awaited_once_with(query="test", limit=3, threshold=None, execution_mode=None)
        self.service.get.assert_not_awaited()

    def test_search_filters_and_response_projection(self):
        item = dict(workflow_id=self.workflow_id, version_id=self.version_id, revision=1,
                    similarity=0.8, search_text="actual search text", workflow=self.request["workflow"],
                    embedding="DO_NOT_EXPOSE")
        self.service.search.return_value = dict(items=[item], returned_count=1, limit=20,
                                               threshold=0.8, embedding_model="bge-m3")
        params = dict(query="test", limit=20, threshold=0.8, execution_mode="static")
        self.check(self.client.post("/workflows/search", json=params), 200, "SearchResponse")
        self.service.search.assert_awaited_once_with(**params)

    def test_recommendation_and_repeated_delete(self):
        self.check(self.client.patch(f"/workflows/{self.workflow_id}/recommendation",
            json={"recommend_yn": "Y", "updated_by": str(self.user)}), 200, "RecommendationResponse")
        self.service.set_recommendation.assert_awaited_once_with(self.workflow_id, recommend_yn="Y", updated_by=self.user)
        for _ in range(2):
            response = self.check(self.client.delete(f"/workflows/{self.workflow_id}",
                params={"deleted_by": str(self.user)}), 204)
            self.assertEqual(response.content, b"")
        self.assertEqual(self.service.delete.await_count, 2)
        self.service.delete.assert_awaited_with(self.workflow_id, deleted_by=self.user)

    def test_request_validation_prevents_service_calls(self):
        cases = [
            ("post", "/workflows/search", {"query": " "}),
            ("post", "/workflows/search", {"query": "x", "limit": 21}),
            ("post", "/workflows/search", {"query": "x", "limit": True}),
            ("post", "/workflows/search", {"query": "x", "threshold": 1.1}),
            ("post", "/workflows/search", {"query": "x", "threshold": "0.8"}),
            ("post", "/workflows/search", {"query": "x", "execution_mode": "bad"}),
            ("post", "/workflows/search", {"query": "x", "extra": 1}),
            ("post", "/workflows", {**self.request, "created_by": "invalid"}),
            ("post", "/workflows", {**self.request, "registration_type": "invalid"}),
            ("post", "/workflows", {**self.request, "workflow": {}}),
            ("get", "/workflows?page=0", None),
            ("get", "/workflows?page_size=101", None),
            ("get", "/workflows?sort_by=invalid", None),
            ("get", "/workflows?recommend_yn=invalid", None),
            ("get", "/workflows/not-uuid", None),
            ("get", f"/workflows/{self.workflow_id}?revision=0", None),
            ("delete", f"/workflows/{self.workflow_id}", None),
            ("patch", f"/workflows/{self.workflow_id}/recommendation", {"recommend_yn": "Y"}),
        ]
        for method, url, body in cases:
            with self.subTest(method=method, url=url, body=body):
                result = self.check(self.client.request(method, url, json=body), 422, "ErrorResponse")
                self.assertEqual(result.json()["error"]["code"], "REQUEST_VALIDATION_ERROR")
        self.assertEqual(self.service.mock_calls, [])

    def test_invalid_idempotency_keys_never_reach_service(self):
        for key in ("", " padded ", "x" * 256):
            with self.subTest(key_length=len(key)):
                self.check(self.client.post("/workflows", json=self.request,
                    headers={"Idempotency-Key": key}), 422, "ErrorResponse")
        self.service.register.assert_not_awaited()

    def test_service_error_mapping_and_no_internal_information(self):
        cases = [
            (WorkflowNotFoundError("DO_NOT_EXPOSE"), 404, "WORKFLOW_NOT_FOUND"),
            (WorkflowVersionNotFoundError("DO_NOT_EXPOSE"), 404, "WORKFLOW_VERSION_NOT_FOUND"),
            (WorkflowRevisionConflictError(1, 2), 409, "WORKFLOW_REVISION_CONFLICT"),
            (WorkflowIdempotencyConflictError("DO_NOT_EXPOSE"), 409, "IDEMPOTENCY_KEY_CONFLICT"),
            (WorkflowValidationError("DO_NOT_EXPOSE"), 422, "WORKFLOW_VALIDATION_ERROR"),
            (WorkflowEmbeddingTimeoutError("DO_NOT_EXPOSE"), 504, "EMBEDDING_TIMEOUT"),
            (WorkflowEmbeddingError("DO_NOT_EXPOSE"), 502, "EMBEDDING_SERVICE_ERROR"),
            (ValueError("DO_NOT_EXPOSE"), 422, "REQUEST_VALIDATION_ERROR"),
            (RuntimeError("DO_NOT_EXPOSE"), 500, "INTERNAL_ERROR"),
        ]
        for exception, status, code in cases:
            with self.subTest(code=code):
                self.service.register.side_effect = exception
                data = self.check(self.client.post("/workflows", json=self.request), status, "ErrorResponse").json()
                self.assertEqual(data["error"]["code"], code)
                if code == "WORKFLOW_REVISION_CONFLICT":
                    self.assertEqual(data["error"]["details"][0]["current_revision"], 2)
                    self.assertEqual(data["error"]["details"][0]["expected_revision"], 1)

    def test_errors_from_every_service_endpoint(self):
        update = {k: v for k, v in self.request.items() if k != "created_by"}
        update.update(updated_by=str(self.user), expected_revision=1)
        cases = [
            ("list", "get", "/workflows", None, RuntimeError("DO_NOT_EXPOSE"), 500),
            ("get", "get", f"/workflows/{self.workflow_id}", None, WorkflowVersionNotFoundError("x"), 404),
            ("search", "post", "/workflows/search", {"query": "x"}, WorkflowEmbeddingTimeoutError("x"), 504),
            ("update", "put", f"/workflows/{self.workflow_id}", update, WorkflowRevisionConflictError(1, 2), 409),
            ("set_recommendation", "patch", f"/workflows/{self.workflow_id}/recommendation",
             {"recommend_yn": "N", "updated_by": str(self.user)}, WorkflowNotFoundError("x"), 404),
            ("delete", "delete", f"/workflows/{self.workflow_id}?deleted_by={self.user}", None, WorkflowNotFoundError("x"), 404),
        ]
        for method, verb, url, body, error, status in cases:
            with self.subTest(method=method):
                getattr(self.service, method).side_effect = error
                self.check(self.client.request(verb, url, json=body), status, "ErrorResponse")

    def test_invalid_service_result_is_safe_internal_error(self):
        self.service.get.return_value = {"unexpected": "DO_NOT_EXPOSE"}
        self.check(self.client.get(f"/workflows/{self.workflow_id}"), 500, "ErrorResponse")

    def test_generated_openapi_operations_match_contract(self):
        actual = self.app.openapi()
        verbs = {"get", "post", "put", "patch", "delete"}
        expected_operations = {(p, m) for p, methods in self.contract["paths"].items() for m in methods if m in verbs}
        actual_operations = {(p, m) for p, methods in actual["paths"].items() for m in methods if m in verbs}
        self.assertEqual(actual_operations, expected_operations)
        self.assertEqual(len(actual_operations), 7)
        for path, method in expected_operations:
            with self.subTest(path=path, method=method):
                operation = actual["paths"][path][method]
                expected = self.contract["paths"][path][method]
                self.assertEqual(operation["operationId"], expected["operationId"])
                self.assertEqual(set(operation["responses"]), set(expected["responses"]))
                self.assertTrue(operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse"))
                self.assertNotIn("401", operation["responses"])
                self.assertNotIn("403", operation["responses"])


if __name__ == "__main__":
    unittest.main()
