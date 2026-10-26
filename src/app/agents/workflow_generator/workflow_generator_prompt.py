from pathlib import Path


_PROMPT_PATH = Path(__file__).with_name("workflow_generator_prompt.md")
_EXTRACT_ARGUMENTS_PATH = Path(__file__).with_name("extract_data_arguments.md")

WORKFLOW_GENERATOR_PROMPT = "\n\n".join((
    _PROMPT_PATH.read_text(encoding="utf-8"),
    _EXTRACT_ARGUMENTS_PATH.read_text(encoding="utf-8"),
))

__all__ = ["WORKFLOW_GENERATOR_PROMPT"]
