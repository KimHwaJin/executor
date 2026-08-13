"""Environment-backed settings for the test Agent."""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class AgentSettings:
    llm_base_url: str
    llm_model: str | None
    llm_api_key: str


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    model = os.getenv("TEST_AGENT_LLM_MODEL", "").strip() or None
    return AgentSettings(
        llm_base_url=os.getenv("TEST_AGENT_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        llm_model=model,
        llm_api_key=os.getenv("TEST_AGENT_LLM_API_KEY", "local-development-only"),
    )
