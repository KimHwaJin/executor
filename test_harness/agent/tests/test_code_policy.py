"""Tests for policy-validated LLM-authored Python cells."""

import pytest
from pydantic import ValidationError

from executor_test_agent.code_policy import PlannedStep


def test_planned_step_accepts_data_analysis_code() -> None:
    step = PlannedStep(
        skill_name="eda",
        tool_name="sum_values",
        code="print(sum(range(1, 11)))",
    )

    assert step.tool_name == "sum_values"


def test_planned_step_rejects_process_access() -> None:
    with pytest.raises(ValidationError, match="blocked module"):
        PlannedStep(
            skill_name="eda",
            tool_name="unsafe_command",
            code="import subprocess\nsubprocess.run(['whoami'])",
        )


@pytest.mark.parametrize("code", ["open('/etc/passwd')", "Path('../../secret')"])
def test_planned_step_rejects_paths_outside_workspace(code: str) -> None:
    with pytest.raises(ValidationError, match=r"filesystem path|traverse"):
        PlannedStep(skill_name="data_io", tool_name="read_file", code=code)
