"""MCP client for Executor that sends the caller identity as headers.

Executor reads the tenant, user, project and session from request headers,
never from the body, and checks X-Api-Key outside local and dev.
"""

from mcp import Client
from mcp.client.streamable_http import (
    create_mcp_http_client,
    streamable_http_client,
)

from executor_test_agent.config import AgentSettings


def executor_headers(
    settings: AgentSettings,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, str]:
    headers = {"X-User-Id": user_id or settings.default_user_id}
    if project_id is not None:
        headers["X-Project-Id"] = project_id
        if session_id is not None:
            headers["X-Session-Id"] = session_id
    if settings.executor_api_key:
        headers["X-Api-Key"] = settings.executor_api_key
    return headers


def executor_client_for(url: str, headers: dict[str, str]) -> Client:
    return Client(
        streamable_http_client(url, http_client=create_mcp_http_client(headers=dict(headers)))
    )


def executor_client(
    settings: AgentSettings,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> Client:
    return executor_client_for(
        settings.executor_mcp_url,
        executor_headers(
            settings,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
        ),
    )
