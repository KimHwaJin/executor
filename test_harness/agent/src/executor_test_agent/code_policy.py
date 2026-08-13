"""Validated Python cell policy for LLM-authored test executions."""

import ast
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BLOCKED_IMPORT_ROOTS = {
    "asyncio",
    "http",
    "httpx",
    "multiprocessing",
    "os",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "urllib",
}
BLOCKED_CALL_NAMES = {"__import__", "breakpoint", "compile", "eval", "exec"}
PATH_CALL_NAMES = {"Path", "open"}
WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class PlannedStep(BaseModel):
    """One policy-validated Jupyter cell authored through an Agent Tool call."""

    model_config = ConfigDict(extra="forbid")

    skill_name: Literal[
        "data_io",
        "data_load",
        "data_preprocess",
        "eda",
        "modeling",
        "evaluation",
        "report",
    ]
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    code: str = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def reject_unsafe_test_code(self) -> "PlannedStep":
        """Reject obvious host, network, process, and dynamic-code access in this test harness."""
        try:
            tree = ast.parse(self.code)
        except SyntaxError as exc:
            raise ValueError("Planned Step code must be valid Python.") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.partition(".")[0] for alias in node.names}
                if roots & BLOCKED_IMPORT_ROOTS:
                    raise ValueError("Planned Step imports a blocked module.")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").partition(".")[0]
                if root in BLOCKED_IMPORT_ROOTS:
                    raise ValueError("Planned Step imports a blocked module.")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in BLOCKED_CALL_NAMES
            ):
                raise ValueError("Planned Step calls a blocked dynamic-code function.")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in PATH_CALL_NAMES
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                path = node.args[0].value.replace("\\", "/")
                if path.startswith(("/", "//")) or WINDOWS_ABSOLUTE_PATH.match(path):
                    raise ValueError("Planned Step uses an absolute filesystem path.")
                if ".." in path.split("/"):
                    raise ValueError("Planned Step path cannot traverse parent directories.")
        return self
