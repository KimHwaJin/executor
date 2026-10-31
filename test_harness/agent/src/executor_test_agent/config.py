"""Environment-backed settings for the test Agent."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AgentSettings:
    llm_base_url: str
    llm_model: str | None
    llm_api_key: str
    executor_mcp_url: str
    executor_redis_url: str
    executor_event_stream: str
    executor_consumer_group_prefix: str
    executor_shared_storage_root: Path
    execution_timeout_seconds: float
    natural_language_execution_enabled: bool
    default_user_id: str
    default_project_id: str
    # Sent as X-Api-Key when Executor checks keys (stg, prd).
    executor_api_key: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    model = os.getenv("TEST_AGENT_LLM_MODEL", "").strip() or None
    return AgentSettings(
        llm_base_url=os.getenv("TEST_AGENT_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        llm_model=model,
        llm_api_key=os.getenv("TEST_AGENT_LLM_API_KEY", "local-development-only"),
        executor_mcp_url=os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp"),
        executor_redis_url=os.getenv("EXECUTOR_REDIS_URL", "redis://127.0.0.1:6379/0"),
        # Executor writes each tenant's events to executor.events.{tenant};
        # local and dev header mode use tenant "dev".
        executor_event_stream=os.getenv(
            "EXECUTOR_EVENT_STREAM",
            f"executor.events.{os.getenv('EXECUTOR_TENANT_ID', 'dev')}",
        ),
        executor_api_key=os.getenv("EXECUTOR_API_KEY") or None,
        executor_consumer_group_prefix=os.getenv(
            "EXECUTOR_AGENT_CONSUMER_GROUP", "executor-test-agent"
        ),
        executor_shared_storage_root=Path(
            os.getenv("EXECUTOR_SHARED_STORAGE_ROOT", "../../shared_dir")
        ),
        execution_timeout_seconds=float(os.getenv("EXECUTOR_EXECUTION_TIMEOUT_SECONDS", "120")),
        natural_language_execution_enabled=os.getenv(
            "TEST_AGENT_ENABLE_NL_EXECUTION", "true"
        ).lower()
        in {"1", "true", "yes", "on"},
        default_user_id=os.getenv("TEST_AGENT_USER_ID", "chat-ui-user"),
        default_project_id=os.getenv("TEST_AGENT_PROJECT_ID", "chat-ui-project"),
    )
