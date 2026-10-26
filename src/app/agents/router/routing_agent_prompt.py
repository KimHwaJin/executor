"""System prompt loader for the Routing Agent."""

from pathlib import Path


_PROMPT_PATH = Path(__file__).with_name("routing_agent_prompt.md")

ROUTING_AGENT_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

__all__ = ["ROUTING_AGENT_PROMPT"]
