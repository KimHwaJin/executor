CONDITIONAL_DECISION_PROMPT = """You decide whether pending conditional Workflow Tools should run.

For every item in `candidates`, inspect only its `condition`, linked
`condition_tool_id`, and the raw observation in `condition_tool_result`.
Return exactly one include/exclude decision for every candidate tool_id.
Do not modify the Workflow, invent Tools, summarize results, or omit candidates.
Use include when the stated condition is supported by the observation; otherwise
use exclude. Give a concise evidence-based reason.
"""

__all__ = ["CONDITIONAL_DECISION_PROMPT"]
