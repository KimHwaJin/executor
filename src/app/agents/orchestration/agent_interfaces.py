"""Small adapters that keep graph nodes independent from agent internals."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from langgraph.constants import CONFIG_KEY_CHECKPOINTER, TAG_NOSTREAM
from pydantic import BaseModel
from app.context.budget import ContextBudget, check_response_length


OutputT = TypeVar("OutputT", bound=BaseModel)


def _unwrap_json_code_fence(content: str) -> str:
    """Remove a single outer ```json fence without changing its JSON body."""
    stripped = content.strip()
    lines = stripped.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().lower() == "```json"
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return content


class InvokableAgent(Protocol):
    def invoke(self, payload: Any) -> Any:
        ...


class AsyncInvokableAgent(InvokableAgent, Protocol):
    async def ainvoke(self, payload: Any) -> Any:
        ...


@dataclass
class JsonMessageAgentAdapter:
    """Adapt a LangChain agent graph to the graph node's JSON invoke contract."""

    agent: Any
    budget: ContextBudget | None = None
    system_prompt: str = ""
    extra_schema: Any = None

    def _bounded_input(self, payload):
        if self.budget:
            payload = self.budget.fit_payload(payload, self.system_prompt, self.extra_schema)
        return self._input(payload)

    @staticmethod
    def _checked_result(result):
        if isinstance(result, dict) and result.get("messages"):
            check_response_length(result["messages"][-1])
        return result

    @staticmethod
    def _input(payload: Any) -> dict:
        return {"messages": [{"role": "user", "content": json.dumps(
            payload, ensure_ascii=False, default=str)}]}

    @staticmethod
    def _config() -> dict:
        # Only validated outer graph messages belong in the user's chat.
        # The outer graph owns checkpoint persistence, including on async calls.
        return {"tags": [TAG_NOSTREAM, "langsmith:nostream"],
                "configurable": {CONFIG_KEY_CHECKPOINTER: None}}

    def invoke(self, payload: Any) -> Any:
        return self._checked_result(self.agent.invoke(self._bounded_input(payload), config=self._config()))

    async def ainvoke(self, payload: Any) -> Any:
        return self._checked_result(await self.agent.ainvoke(self._bounded_input(payload), config=self._config()))


def invoke_typed(
    agent: InvokableAgent,
    payload: BaseModel | dict[str, Any],
    output_type: type[OutputT],
) -> OutputT:
    raw_payload = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    result = agent.invoke(raw_payload)
    return _validate_typed_result(result, output_type)


async def ainvoke_typed(
    agent: AsyncInvokableAgent,
    payload: BaseModel | dict[str, Any],
    output_type: type[OutputT],
) -> OutputT:
    raw_payload = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    result = await agent.ainvoke(raw_payload)
    return _validate_typed_result(result, output_type)


def _validate_typed_result(result: Any, output_type: type[OutputT]) -> OutputT:
    if isinstance(result, dict) and "structured_response" in result:
        result = result["structured_response"]
    elif isinstance(result, dict) and result.get("messages"):
        content = getattr(result["messages"][-1], "content", None)
        if isinstance(content, list):
            text_blocks = [
                block.get("text")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            content = text_blocks[-1] if text_blocks else None
        if isinstance(content, str) and content.strip():
            return output_type.model_validate_json(
                _unwrap_json_code_fence(content)
            )
    return result if isinstance(result, output_type) else output_type.model_validate(result)


@dataclass
class StructuredLLMAgent:
    """LangChain model adapter with a Pydantic structured output contract."""

    model: Any
    system_prompt: str
    output_type: type[BaseModel]
    method: str = "prompt_json"
    max_validation_attempts: int = 3
    budget: ContextBudget | None = None

    def _prepare(self, payload: Any):
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = self.system_prompt
        model = self.model
        if self.method == "provider_json_schema":
            model = model.with_structured_output(self.output_type, method="json_schema")
        elif self.method == "prompt_json":
            schema = json.dumps(self.output_type.model_json_schema(), ensure_ascii=False)
            prompt += (
                "\n\nReturn exactly one JSON object matching this JSON Schema. "
                "Include every required field and do not return markdown or "
                f"explanatory text.\nJSON Schema: {schema}"
            )
        else:
            raise ValueError(f"Unsupported structured output method: {self.method!r}")
        if self.budget:
            payload = self.budget.fit_payload(payload, prompt,
                self.output_type.model_json_schema() if self.method == "provider_json_schema" else None)
        return model, [SystemMessage(content=prompt), HumanMessage(
            content=json.dumps(payload, ensure_ascii=False, default=str))]

    def _parse(self, response: Any) -> BaseModel:
        content = getattr(response, "content", response)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Structured LLM response must contain JSON text")
        return self.output_type.model_validate_json(_unwrap_json_code_fence(content))

    @staticmethod
    def _add_feedback(messages: list, response: Any, error: ValueError) -> None:
        from langchain_core.messages import HumanMessage

        messages.extend([response, HumanMessage(content=(
            "The previous response failed JSON/Pydantic validation. "
            "Return the complete corrected JSON object only. "
            f"Validation error: {error}"
        ))])

    def _exhausted(self, error: Exception | None) -> ValueError:
        return ValueError("Structured LLM response failed validation after "
                          f"{self.max_validation_attempts} attempts: {error}")

    def invoke(self, payload: Any) -> BaseModel:
        model, messages = self._prepare(payload)
        if self.method == "provider_json_schema":
            return model.invoke(messages)
        last_error = None
        for _ in range(self.max_validation_attempts):
            if self.budget:
                self.budget.check(messages)
            response = model.invoke(messages)
            check_response_length(response)
            try:
                return self._parse(response)
            except ValueError as exc:
                last_error = exc
                self._add_feedback(messages, response, exc)
        raise self._exhausted(last_error) from last_error

    async def ainvoke(self, payload: Any) -> BaseModel:
        model, messages = self._prepare(payload)
        if self.method == "provider_json_schema":
            return await model.ainvoke(messages)
        last_error = None
        for _ in range(self.max_validation_attempts):
            if self.budget:
                self.budget.check(messages)
            response = await model.ainvoke(messages)
            check_response_length(response)
            try:
                return self._parse(response)
            except ValueError as exc:
                last_error = exc
                self._add_feedback(messages, response, exc)
        raise self._exhausted(last_error) from last_error


@dataclass
class LabelOnlyLLMAgent:
    """Classify with plain text and build the typed result in Python."""

    model: Any
    system_prompt: str
    output_type: type[BaseModel]
    label_field: str
    allowed_labels: tuple[str, ...]
    budget: ContextBudget | None = None

    def _messages(self, payload: Any) -> list:
        from langchain_core.messages import HumanMessage, SystemMessage

        labels = "\n".join(self.allowed_labels)
        prompt = (
            f"{self.system_prompt}\n\n"
            "Classify the request and return exactly one label from the list "
            "below. Return only the label, with no JSON, markdown, reason, or "
            f"other text.\n{labels}"
        )
        if self.budget:
            payload = self.budget.fit_payload(payload, prompt)
        return [
            SystemMessage(content=prompt),
            HumanMessage(
                content=json.dumps(payload, ensure_ascii=False, default=str)
            ),
        ]

    def _parse(self, response: Any):
        check_response_length(response)
        raw_label = response.content if hasattr(response, "content") else response
        if not isinstance(raw_label, str):
            raise ValueError("LLM label response must be plain text")
        label = raw_label.strip().strip("`\"'").strip()
        if label not in self.allowed_labels:
            raise ValueError(
                f"Unexpected LLM label for {self.label_field}: {raw_label!r}"
            )
        return self.output_type.model_validate(
            {
                self.label_field: label,
                "reason": f"LLM classified the request as {label}.",
            }
        )

    def invoke(self, payload: Any):
        return self._parse(self.model.invoke(self._messages(payload)))

    async def ainvoke(self, payload: Any):
        return self._parse(await self.model.ainvoke(self._messages(payload)))


@dataclass
class SimpleLLMAgent:
    """Invoke a chat model directly for plain text question answering."""

    model: Any
    system_prompt: str
    budget: ContextBudget | None = None

    def _messages(self, payload: Any) -> list:
        from langchain_core.messages import HumanMessage, SystemMessage

        if not isinstance(payload, dict):
            raise TypeError("SimpleLLMAgent payload must be a dictionary")
        user_request = str(payload.get("user_request") or "").strip()
        if not user_request:
            raise ValueError("user_request is required")

        if self.budget:
            payload = self.budget.fit_payload(payload, self.system_prompt)
        content = user_request if set(payload) == {"user_request"} else json.dumps(payload, ensure_ascii=False, default=str)
        messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=content)]
        if self.budget:
            self.budget.check(messages)
        return messages

    def _parse(self, response: Any):
        check_response_length(response)
        answer = response.content if hasattr(response, "content") else response
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("LLM response must contain a non-empty text answer")
        return {"answer": answer.strip()}

    def invoke(self, payload: Any):
        return self._parse(self.model.invoke(self._messages(payload)))

    async def ainvoke(self, payload: Any):
        return self._parse(await self.model.ainvoke(self._messages(payload)))


@dataclass
class PlaceholderAgent:
    message: str

    def invoke(self, payload: Any) -> dict[str, str]:
        return {"status": "placeholder", "message": self.message}

    async def ainvoke(self, payload: Any) -> dict[str, str]:
        return self.invoke(payload)
