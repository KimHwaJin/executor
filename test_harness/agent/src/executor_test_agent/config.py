"""Environment-backed settings for the test Agent."""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class AgentSettings:
    llm_base_url: str
    llm_model: str | None
    llm_api_key: str
    executor_mcp_url: str
    executor_redis_url: str
    executor_event_stream: str
    executor_consumer_group_prefix: str
    execution_timeout_seconds: float
    natural_language_execution_enabled: bool
    default_user_id: str
    default_project_id: str


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    model = os.getenv("TEST_AGENT_LLM_MODEL", "").strip() or None
    return AgentSettings(
        llm_base_url=os.getenv("TEST_AGENT_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        llm_model=model,
        llm_api_key=os.getenv("TEST_AGENT_LLM_API_KEY", "local-development-only"),
        executor_mcp_url=os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp"),
        executor_redis_url=os.getenv("EXECUTOR_REDIS_URL", "redis://127.0.0.1:6379/0"),
        executor_event_stream=os.getenv("EXECUTOR_EVENT_STREAM", "executor.events"),
        executor_consumer_group_prefix=os.getenv(
            "EXECUTOR_AGENT_CONSUMER_GROUP", "executor-test-agent"
        ),
        execution_timeout_seconds=float(os.getenv("EXECUTOR_EXECUTION_TIMEOUT_SECONDS", "120")),
        natural_language_execution_enabled=os.getenv(
            "TEST_AGENT_ENABLE_NL_EXECUTION", "true"
        ).lower()
        in {"1", "true", "yes", "on"},
        default_user_id=os.getenv("TEST_AGENT_USER_ID", "chat-ui-user"),
        default_project_id=os.getenv("TEST_AGENT_PROJECT_ID", "chat-ui-project"),
    )
