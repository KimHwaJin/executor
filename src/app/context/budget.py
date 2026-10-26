"""Offline token budgeting with one shared BPE tokenizer across chat models.

Counts use cl100k_base, not each server's tokenizer or billing usage.
Required inputs fail explicitly rather than silently losing confirmed values.
"""
from __future__ import annotations

import json
import logging
import base64
import hashlib
from functools import lru_cache
from pathlib import Path
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def context_tokenizer():
    import tiktoken
    raw = Path(__file__).with_name("tokenizers").joinpath("cl100k_base.tiktoken").read_bytes()
    if hashlib.sha256(raw).hexdigest() != "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7":
        raise ValueError("Context tokenizer asset checksum mismatch")
    ranks = {base64.b64decode(token): int(rank) for token, rank in
             (line.split() for line in raw.splitlines() if line)}
    return tiktoken.Encoding(
        name="cl100k_base",
        pat_str=r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s",
        mergeable_ranks=ranks,
        special_tokens={"<|endoftext|>": 100257, "<|fim_prefix|>": 100258,
                        "<|fim_middle|>": 100259, "<|fim_suffix|>": 100260,
                        "<|endofprompt|>": 100276},
    )


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else dumps(value)
    return len(context_tokenizer().encode(text, disallowed_special=()))


def token_chunks(text: str, limit: int) -> list[str]:
    """Split tokenized text without corrupting Korean/emoji character boundaries."""
    if limit < 4:
        raise ValueError("Token chunk limit must be at least 4")
    tokenizer = context_tokenizer()
    tokens = tokenizer.encode(text, disallowed_special=())
    chunks, start = [], 0
    while start < len(tokens):
        end = min(start + limit, len(tokens))
        while end > start:
            try:
                chunk = tokenizer.decode(tokens[start:end], errors="strict")
            except UnicodeDecodeError:
                end -= 1
                continue
            if estimate_tokens(chunk) <= limit:
                break
            end -= 1
        if end == start:
            raise ValueError("Token budget cannot fit the next Unicode character")
        chunks.append(chunk)
        start = end
    return chunks


class ContextBudgetExceeded(ValueError):
    """Required context cannot fit without changing its meaning."""


@dataclass(frozen=True)
class ContextBudget:
    input_limit: int = 32000

    def __post_init__(self):
        if type(self.input_limit) is not int or self.input_limit <= 0:
            raise ValueError("Context input token budget must be a positive integer")

    def count_messages(self, messages: list, extra: Any = None) -> int:
        return 32 + sum(32 + estimate_tokens(
            message if isinstance(message, dict) else message.model_dump(exclude_none=True)
        ) for message in messages) + (estimate_tokens(extra) if extra else 0)

    def check(self, messages: list, extra: Any = None) -> None:
        count = self.count_messages(messages, extra)
        if count > self.input_limit:
            raise ContextBudgetExceeded(
                f"Estimated model input {count} exceeds budget {self.input_limit}; "
                "narrow the requested analyses/tools or adjust CONTEXT_INPUT_BUDGET_TOKENS."
            )

    def fit_payload(self, payload: Any, system_prompt: str, extra: Any = None) -> Any:
        """Trim report excerpts only; never silently discard conversation memory."""
        from copy import deepcopy
        from langchain_core.messages import HumanMessage, SystemMessage
        result = deepcopy(payload)
        def fits():
            return self.count_messages([
                SystemMessage(content=system_prompt), HumanMessage(content=dumps(result))
            ], extra) <= self.input_limit
        if isinstance(result, dict):
            context = result.get("session_context")
            if isinstance(context, dict):
                # Unsummarized turns and the successful summary are required.
                # Failed summarization must not become a silent memory gap.
                # Report originals remain retrievable by message ID.
                for report in context.get("reports") or []:
                    while not fits() and len(report.get("content", "")) > 512:
                        text = report["content"]
                        report["content"] = text[:len(text) // 2] + "\n[보고서 일부 생략; 원본 message_id 참조]"
                        report["truncated"] = True
        if not fits():
            raise ContextBudgetExceeded(
                "Required prompt, schemas and analysis evidence exceed the model input budget"
            )
        logger.debug("Model input budget: estimate=%s limit=%s counter=cl100k_base",
                     self.count_messages([SystemMessage(content=system_prompt), HumanMessage(content=dumps(result))], extra),
                     self.input_limit)
        return result


def check_response_length(response: Any) -> None:
    metadata = getattr(response, "response_metadata", {}) or {}
    logger.debug("Model response usage=%s finish_reason=%s", getattr(response, "usage_metadata", None)
                 or metadata.get("token_usage"), metadata.get("finish_reason") or metadata.get("stop_reason"))
    if metadata.get("finish_reason") in {"length", "max_tokens"} or metadata.get("stop_reason") == "max_tokens":
        raise ContextBudgetExceeded("Model response reached its output token limit; output is incomplete")
