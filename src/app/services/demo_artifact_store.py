"""Local demo artifact paths and JSON persistence for one analysis run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_config import PROJECT_ROOT, AgentSettings


def _path_segment(value: str, label: str) -> str:
    segment = value.strip()
    if (
        not segment
        or segment in {".", ".."}
        or any(character in segment for character in '<>:"/\\|?*')
        or any(ord(character) < 32 for character in segment)
    ):
        raise ValueError(f"{label} cannot be used as an artifact path segment")
    return segment


def build_run_artifact_dir(
    settings: AgentSettings,
    *,
    user_id: str,
    project_id: str,
    session_id: str,
    task_id: str,
) -> Path:
    """Return a validated task directory under the configured demo root."""
    root = settings.demo_artifacts_root.resolve()
    path = root.joinpath(
        _path_segment(user_id, "user_id"),
        _path_segment(project_id, "project_id"),
        _path_segment(session_id, "session_id"),
        _path_segment(task_id, "task_id"),
    ).resolve()
    if not path.is_relative_to(root):
        raise ValueError("artifact path must stay inside demo_artifacts_root")
    return path


def build_workflow_candidate_artifact_path(
    settings: AgentSettings,
    *,
    session_id: str,
    task_id: str,
    workflow_id: str,
) -> Path:
    """Return a debug artifact path for a selected Workflow candidate."""
    root = settings.demo_artifacts_root.resolve()
    workflows_dir = root.joinpath("workflows").resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = workflows_dir.joinpath(
        "__".join(
            [
                _path_segment(session_id, "session_id"),
                _path_segment(task_id, "task_id"),
                _path_segment(workflow_id, "workflow_id"),
                timestamp,
            ]
        )
        + ".json"
    ).resolve()
    if not path.is_relative_to(root):
        raise ValueError("artifact path must stay inside demo_artifacts_root")
    return path

def build_workflow_output_dir(settings: AgentSettings, state: dict[str, Any]) -> str:
    """Build the output_dir value embedded in Workflow context."""
    path = build_run_artifact_dir(
        settings,
        user_id=state["user_id"],
        project_id=state["project_id"],
        session_id=state["session_id"],
        task_id=state["task_id"],
    )
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_demo_json(path: Path, payload: Any) -> Path:
    """Write one human-readable UTF-8 JSON demo artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "build_run_artifact_dir",
    "build_workflow_candidate_artifact_path",
    "build_workflow_output_dir",
    "write_demo_json",
]
