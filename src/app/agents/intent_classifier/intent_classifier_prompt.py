"""System prompt loader for analysis-intent classifier Agent."""

from pathlib import Path


_PROMPT_PATH = Path(__file__).with_name("intent_classifier_prompt.md")

ANALYSIS_INTENT_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

__all__ = ["ANALYSIS_INTENT_PROMPT"]