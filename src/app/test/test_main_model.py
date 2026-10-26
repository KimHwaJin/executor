"""Server-owned model configuration and API model-field removal."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from pydantic import ValidationError

from agent_config import (load_agent_settings, resolve_main_model,
                          with_main_model, MAIN_MODEL_PRESETS)
from app.agents.orchestration.dependencies import create_chat_model
from app.schemas.common.workflow_run_schema import WorkflowRunRequest
from app.services import workflow_run_service as service


def settings():
    return load_agent_settings({"MODEL_API_KEY": "test", "API_BASE_URL": "http://model.test/v1"})


class MainModelTests(unittest.IsolatedAsyncioTestCase):
    def test_server_configuration_and_request_contract(self):
        cfg = settings()
        self.assertEqual(cfg.model_name, "GLM-5.3")
        self.assertEqual(cfg.main_model["parameters"]["reasoning_effort"], "low")
        for name in ("GaiA-Small-Latest", "new-deployed-model"):
            with patch("agent_config.USER_LLM_MODEL", name), patch("agent_config.MAIN_MODEL_CONFIG", {"parameters": {"temperature": 0.2}}):
                configured = settings()
                self.assertEqual(configured.model_name, MAIN_MODEL_PRESETS.get(name, {}).get("model", name))
                self.assertEqual(configured.main_model["parameters"]["temperature"], 0.2)
                self.assertEqual(configured.embedding_model_name, cfg.embedding_model_name)
        body = {"input": {"content": [{"type": "text", "text": "hello"}]}}
        self.assertNotIn("main_model", WorkflowRunRequest(**body).model_dump())
        from app.schemas.common.workflow_task_schema import TaskSnapshot
        for schema in (WorkflowRunRequest, TaskSnapshot):
            self.assertNotIn("main_model", schema.model_json_schema()["properties"])
            self.assertNotIn("main_model_name", schema.model_json_schema()["properties"])
        for field, value in (("main_model", {"name": "GaiA-Small-Latest"}), ("main_model_name", "default"), ("main_model", None)):
            with self.assertRaises(ValidationError):
                WorkflowRunRequest(**body, **{field: value})
            with self.assertRaises(ValidationError):
                WorkflowRunRequest(task_id=uuid4(), run_id=uuid4(), **{field: value},
                    command={"resume": {"action": "edit", "interaction_id": uuid4(), "revision": 1}})

    def test_environment_model_name_overrides_code_with_matching_parameters(self):
        for name in ("GaiA-Small-Latest", "gaia-Qwen3.6-35B-A3B"):
            cfg = load_agent_settings({"LLM_MODEL_NAME": name, "MODEL_ENABLE_THINKING": "true"})
            self.assertEqual(cfg.model_name, "gaia-Qwen3.6-35B-A3B")
            self.assertFalse(cfg.main_model["parameters"]["enable_thinking"])
            self.assertEqual(cfg.main_model["parameters"]["top_k"], 20)
            self.assertNotIn("reasoning_effort", cfg.main_model["parameters"])
        with patch("agent_config.USER_LLM_MODEL", "GaiA-Medium-Latest"), patch("agent_config.MAIN_MODEL_CONFIG", {"parameters": {"temperature": 0.4}}):
            for env in ({}, {"LLM_MODEL_NAME": " "}):
                self.assertEqual(load_agent_settings(env).model_name, "GLM-5.3-Flash")
            cfg = load_agent_settings({"LLM_MODEL_NAME": "custom-model", "MODEL_TEMPERATURE": "0"})
            self.assertEqual(cfg.model_name, "custom-model")
            self.assertEqual(cfg.main_model["parameters"], {"temperature": 0.4})


    async def test_real_sdk_wire_payloads(self):
        for alias, preset in MAIN_MODEL_PRESETS.items():
            captured = []
            async def handle(request):
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                    "model": preset["model"], "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"}}]})
            config = with_main_model(settings(), resolve_main_model({"name": alias,
                "parameters": {"temperature": 0.3, "custom_option": {"enabled": False}}}))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                from langchain_openai import ChatOpenAI
                with patch("langchain_openai.ChatOpenAI", side_effect=lambda **kw: ChatOpenAI(**kw, http_async_client=client)):
                    model = create_chat_model(config)
                await model.ainvoke("hello")
            body = captured[0]
            self.assertEqual(body["model"], preset["model"])
            self.assertEqual(body["temperature"], 0.3)
            self.assertEqual(body["top_p"], 0.95)
            self.assertEqual(body["custom_option"], {"enabled": False})
            self.assertNotIn("max_tokens", body)
            self.assertNotIn("max_completion_tokens", body)
            if alias == "GaiA-Small-Latest":
                self.assertFalse(body["chat_template_kwargs"]["enable_thinking"])
                self.assertEqual(body["top_k"], 20)
                self.assertNotIn("reasoning_effort", body)
            else:
                self.assertEqual(body["reasoning_effort"], "low")
                self.assertFalse(body["chat_template_kwargs"]["clear_thinking"])
                self.assertNotIn("enable_thinking", body["chat_template_kwargs"])

    async def test_resume_ignores_old_saved_model(self):
        selection = resolve_main_model({"name": "GaiA-Small-Latest", "parameters": {"temperature": 0.4}})
        task_id, run_id, session_id = uuid4(), uuid4(), uuid4()
        origin = SimpleNamespace(task_id=task_id, run_id=run_id, session_id=session_id,
            status=service.AgentRunStatus.INTERRUPTED, metadata_json={"main_model": selection})
        payload = WorkflowRunRequest(task_id=task_id, run_id=run_id,
            command={"resume": {"action": "edit", "interaction_id": uuid4(), "revision": 1}})
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[object(), None, origin]))
        with patch.object(service, "owned_run", AsyncMock(return_value=origin)), \
             patch.object(service.SessionRepository, "get_active_by_user", AsyncMock()), \
             patch.object(service, "resume_command", return_value={"resume": {}}), \
             patch.object(service.RunService, "create", AsyncMock()) as create:
            await service.submit_workflow(payload, "key", uuid4(), db)
        self.assertNotIn("main_model", create.call_args.args[3].metadata)

    async def test_new_submission_does_not_store_model(self):
        payload = WorkflowRunRequest(session_id=uuid4(), input={"content": [{"type": "text", "text": "hello"}]})
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[object(), None]))
        saved = SimpleNamespace(message=SimpleNamespace(message_id=uuid4(), content_text="hello"))
        with patch.object(service.MessageService, "create", AsyncMock(return_value=saved)), \
             patch.object(service.RunService, "create", AsyncMock()) as create:
            await service.submit_workflow(payload, "new-key", uuid4(), db)
        self.assertNotIn("main_model", create.call_args.args[3].metadata)

    async def test_graph_boundary_does_not_inject_model(self):
        from app.services import agent_graph_service as graph_service
        with patch.object(graph_service, "ainvoke_with_crud_message_persistence", AsyncMock(return_value={})) as invoke:
            await graph_service.ainvoke_user_turn(None, user_id=uuid4(), project_id=uuid4(),
                session_id=uuid4(), run_id=uuid4(), user_request="hello", graph=object())
            self.assertNotIn("main_model", invoke.call_args.args[1])
            await graph_service.ainvoke_resume(None, user_id=uuid4(), session_id=uuid4(),
                checkpoint_run_id=uuid4(), command={}, graph=object())
            self.assertIsNone(invoke.call_args.args[1].update)


if __name__ == "__main__":
    unittest.main()
