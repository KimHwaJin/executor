import asyncio
import logging
import typing import List, Optional


from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from common.config import config
from common.logger import init_logger
from common.response_formatter import (
    ainvoke_graph,
    build_bot_response,
    build_openai_chat_completion_response,
    stream_graph,
    stream_openai_chat_completion,
)
from routers.chat.enum import GaiaChatType
from routers.chat.module import (
    WorkflowManagerNotFoundError,
    WorkflowNotFoundError,
    get_workflow_manager,
    list_workflow_errors,
    list_workflows,
)

class UserChatBase(BaseModel):
    message: str
    user_id: str

class RemoteUrl(BaseModel):
    """Gaia Platform의 Super Agent Chat에서 전달된 선택된 Agent의 연결 정보"""
    url: str
    token: str

class UserChatGaia(UserChatBase):
    session_id: str
    chat_type: GaiaChatType = GaiaChatType.DEFAULT
    a2a_remove_urls: Optional[List[RemoteUrl]] = None
    is_super_agent: Optional[bool] = False
    main_model_name: Optional[str] = None
    session_system_prompt: Optional[str] = None

init_logger()
logger = logging.getLogger(__name__)

gaia_router = APIRouter(prefix="", tags=["GAIA"])
api_router = APIRouter(prefix="", tags=["API"])
cube_router = APIRouter(prefix="", tags=["CUBE"])

DEFALUT_WORKFLOW_ALIAS = "default"


def _request_extra(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "message"}

def _workflow_response() -> dict:
    default_workflow = getattr(config, "DEFAULT_WORKFLOW", "") or ""
    workflows = list_workflows()
    return {
        "default_workflow": default_workflow,
        "defaultWorkflow": default_workflow,
        "default_available": default_workflow in workflows,
        "workflows": workflows,
        "load_errors": list_workflow_errors(),
    }

@api_router.get("/workflows")
async def workflow_list():
    return _workflow_response()

@gaia_router.post("/{workflow}/gaia/completion")
async def gaia_caht(workflow: str, user_chat: UserChatGaia, request: Request):
    graph = _get_graph_or_404(workflow)
    body = await request.json()
    session_id = user_chat.session_id

    payload = {
        "query": user_chat.message,
        "user_id": user_chat.user_id,
        "session_id": session_id,
        "gaia_session_name": "",
        "gaia_input_channel": "gaia",
        "chat_type": user_chat.chat_type,
        "a2a_remote_urls": _remote_urls(user_chat.a2a_remote_urls),
        "is_super_agent": user_chat.is_super_agent,
        "main_model_name": user_chat.main_model_name,
        "session_system_prompt": user_chat.session_system_prompt,
    }
    payload.update(_request_extra(body))

    if config.GAIA_OUTPUT_STREAM:
        return StreamingResponse(stream_graph(graph, payload), media_type="text/event-stream")

    result = await _await_with_disconnect(ainvoke_graph(graph, payload), request)
    if result is None:
        return None
    return build_bot_response(result, session_id=session_id, user_id=user_chat.user_id)

@api_router.post(
    "/{workflow}/api/completion",
    summary="OpenAI Chat Completion",
    description = "GAIA completion input을 받아 OpenAI Chat Completions compatible response로 반환합니다."
)
async def api_chat(workflow: str, user_chat: UserChatGaia, request: Request):
    graph = _get_graph_or_404(workflow)
    body = await request.json()
    session_id = user_chat.session_id

    payload = {
        "query": user_chat.message,
        "user_id": user_chat.user_id,
        "session_id": session_id,
        "gaia_session_name": config.GAIA_API_SESSION_NAME,
        "gaia_input_channel": "api",
        "chat_type": user_chat.chat_type,
        "a2a_remote_urls": _remote_urls(user_chat.a2a_remote_urls),
        "is_super_agent": user_chat.is_super_agent,
        "main_model_name": user_chat.main_model_name,
        "session_system_prompt": user_chat.session_system_prompt,
    }
    payload.update(_request_extra(body))

    if config.API_OUTPUT_STREAM:
        return StreamingResponse(stream_openai_chat_completion(graph, payload), media_type="text/event-stream")

    result = await ainvoke_graph(graph, payload)
    return build_openai_chat_completion_response(result, payload)