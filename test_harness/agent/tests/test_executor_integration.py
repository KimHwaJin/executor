"""Tests for consolidated Executor MCP result collection used by the test Agent."""

from typing import Any, cast

from executor_test_agent.integrations import executor as executor_module


async def test_collect_execution_result_hydrates_normalized_outputs(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_required_result(_client, tool, arguments):
        calls.append((tool, arguments))
        if tool == "execution_result_get":
            return {
                "execution": {"execution_id": "execution-1"},
                "operations": [
                    {
                        "steps": [
                            {
                                "result": {
                                    "result_ref": {
                                        "execution_id": "execution-1",
                                        "step_id": "step-1",
                                        "attempt_id": "attempt-1",
                                    }
                                }
                            }
                        ]
                    }
                ],
            }
        if tool == "execution_output_list":
            return {
                "items": [
                    {
                        "output_id": "output-1",
                        "kind": "STREAM",
                        "produced_by": {
                            "step_id": "step-1",
                            "attempt_id": "attempt-1",
                        },
                        "representations": [
                            {
                                "representation_id": "representation-1",
                                "media_type": "text/plain",
                            }
                        ],
                    }
                ],
                "next_cursor": None,
            }
        assert tool == "execution_output_content_get"
        return {
            "delivery": "INLINE",
            "content": "55\n",
            "content_url": "/content",
        }

    monkeypatch.setattr(executor_module, "required_tool_result", fake_required_result)

    result = await executor_module.collect_execution_result(cast(Any, object()), "execution-1")

    assert result["execution"]["execution_id"] == "execution-1"
    resolved = result["operations"][0]["steps"][0]["result"]["resolved_outputs"]
    assert resolved[0]["representations"][0]["content"] == "55\n"
    assert [tool for tool, _arguments in calls] == [
        "execution_result_get",
        "execution_output_list",
        "execution_output_content_get",
    ]
