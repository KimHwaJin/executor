"""Report generator agent construction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from app.context.budget import ContextBudget, check_response_length


@dataclass
class MarkdownReportAgent:
    model: Any
    system_prompt: str
    budget: ContextBudget | None = None

    def _messages(self, payload: Any) -> list:
        from langchain_core.messages import HumanMessage, SystemMessage

        if self.budget:
            payload = self.budget.fit_payload(payload, self.system_prompt)
        return (
            [
                SystemMessage(content=self.system_prompt),
                HumanMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str)
                ),
            ]
        )

    def _parse(self, response: Any) -> dict[str, str]:
        check_response_length(response)
        content = response.content if hasattr(response, "content") else response
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Report generator must return non-empty markdown text")
        return {"content": content.strip()}

    def invoke(self, payload: Any) -> dict[str, str]:
        return self._parse(self.model.invoke(self._messages(payload)))

    async def ainvoke(self, payload: Any) -> dict[str, str]:
        return self._parse(await self.model.ainvoke(self._messages(payload)))


def create_report_generator_agent(model: Any, *, budget: ContextBudget | None = None):
    prompt_path = Path(__file__).with_name("report_generator_prompt.md")
    return MarkdownReportAgent(
        model=model,
        budget=budget,
        system_prompt=prompt_path.read_text(encoding="utf-8"),
    )


__all__ = ["MarkdownReportAgent", "create_report_generator_agent"]
