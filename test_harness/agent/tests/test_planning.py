"""Natural-language planning boundary tests."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from executor_test_agent.planning import plan_message


class FakeModel:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def ainvoke(self, input):
        assert input[0].type == "system"
        assert input[-1].content == "Calculate the sum."
        return AIMessage(content=json.dumps(self.payload))


async def test_plan_message_validates_an_execution_plan() -> None:
    decision = await plan_message(
        FakeModel(
            {
                "intent": "EXECUTE",
                "response": "합계를 계산합니다.",
                "runtime_profile": "basic",
                "steps": [
                    {
                        "skill_name": "eda",
                        "tool_name": "sum_values",
                        "code": "print(sum(range(1, 11)))",
                    }
                ],
            }
        ),
        [HumanMessage(content="Calculate the sum.")],
    )

    assert decision.intent == "EXECUTE"
    assert decision.steps[0].tool_name == "sum_values"


async def test_plan_message_accepts_a_chat_answer() -> None:
    decision = await plan_message(
        FakeModel(
            {
                "intent": "CHAT",
                "response": "안녕하세요.",
                "runtime_profile": "basic",
                "steps": [],
            }
        ),
        [HumanMessage(content="Calculate the sum.")],
    )

    assert decision.intent == "CHAT"
    assert decision.response == "안녕하세요."


async def test_plan_message_rejects_process_access() -> None:
    with pytest.raises(ValidationError, match="blocked module"):
        await plan_message(
            FakeModel(
                {
                    "intent": "EXECUTE",
                    "response": "실행합니다.",
                    "runtime_profile": "basic",
                    "steps": [
                        {
                            "skill_name": "eda",
                            "tool_name": "unsafe_command",
                            "code": "import subprocess\nsubprocess.run(['whoami'])",
                        }
                    ],
                }
            ),
            [HumanMessage(content="Calculate the sum.")],
        )
