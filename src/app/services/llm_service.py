from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import settings
from app.schemas.common.llm_schema import LLMCompletionRequest, LLMCompletionResult


class LLMProviderError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LLMCall:
    result: LLMCompletionResult
    latency_ms: int


class LLMClient:
    """Mock 또는 OpenAI 호환 chat/completions 호출 클라이언트."""

    def __init__(self, transport: Callable[[Request, float], bytes] | None = None):
        self._transport = transport or self._urlopen

    @staticmethod
    def _urlopen(request: Request, timeout: float) -> bytes:
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    async def complete(self, payload: LLMCompletionRequest, *, provider: str | None = None) -> LLMCall:
        started = perf_counter()
        selected_provider = (provider or settings.llm_provider).lower()
        if selected_provider == "mock":
            last_user = next((m.content for m in reversed(payload.messages) if m.role == "user"), "")
            content = f"요청하신 메시지에 대한 테스트 LLM 답변입니다: {last_user}"
            raw = {
                "id": "mock-completion",
                "model": payload.model,
                "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            return LLMCall(result=self._parse(raw), latency_ms=int((perf_counter() - started) * 1000))

        if not settings.llm_api_key:
            raise LLMProviderError("LLM-CONFIG-001", "LLM_API_KEY가 설정되지 않았습니다.")

        body = {
            "model": payload.model,
            "messages": [m.model_dump() for m in payload.messages],
            "temperature": payload.temperature,
            "max_tokens": payload.max_tokens,
        }
        request = Request(
            f"{settings.llm_api_base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            raw_bytes = await asyncio.to_thread(self._transport, request, settings.llm_timeout_seconds)
            raw = json.loads(raw_bytes.decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMProviderError("LLM-HTTP-001", f"LLM HTTP {exc.code}: {detail[:1000]}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise LLMProviderError("LLM-CONNECTION-001", f"LLM 연결 실패: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMProviderError("LLM-RESPONSE-001", "LLM 응답이 유효한 JSON이 아닙니다.") from exc
        return LLMCall(result=self._parse(raw), latency_ms=int((perf_counter() - started) * 1000))

    @staticmethod
    def _parse(raw: dict[str, Any]) -> LLMCompletionResult:
        try:
            choice = raw["choices"][0]
            content = choice["message"]["content"].strip()
            usage = raw.get("usage") or {}
            if not content:
                raise ValueError("empty content")
            return LLMCompletionResult(
                content=content,
                raw_response=raw,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                finish_reason=choice.get("finish_reason"),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMProviderError("LLM-RESPONSE-002", "LLM 응답에 assistant content가 없습니다.") from exc


llm_client = LLMClient()
