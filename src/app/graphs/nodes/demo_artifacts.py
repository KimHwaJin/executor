"""Persist approved Workflow JSON for local demo inspection."""

from __future__ import annotations

import logging

from agent_config import AgentSettings
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.services.demo_artifact_store import (
    build_run_artifact_dir,
    write_demo_json,
)


logger = logging.getLogger(__name__)


def make_save_approved_workflow(settings: AgentSettings):
    def save_approved_workflow(state: AnalysisWorkflowState) -> dict:
        task_id = state.get("task_id")
        logger.debug("[DEBUG] ENTER save_approved_workflow task_id=%s", task_id)
        if not state.get("approval", {}).get("approved"):
            raise ValueError("only an approved Workflow can be saved")
        if state.get("workflow_definition") or not settings.demo_artifacts_enabled:
            logger.debug(
                "[DEBUG] Approved workflow save skipped task_id=%s demo_artifacts_enabled=false",
                task_id,
            )
            logger.debug("[DEBUG] EXIT save_approved_workflow task_id=%s", task_id)
            return {}

        run_dir = build_run_artifact_dir(
            settings,
            user_id=state["user_id"],
            project_id=state["project_id"],
            session_id=state["session_id"],
            task_id=state["task_id"],
        )
        path = write_demo_json(run_dir / "workflow.json", state["workflow"])
        logger.debug(
            "[DEBUG] Approved workflow save complete task_id=%s absolute_path=%s",
            task_id, path.resolve(),
        )
        result = {
            "artifact_files": {
                **state.get("artifact_files", {}),
                "workflow": str(path),
            }
        }
        logger.debug("[DEBUG] EXIT save_approved_workflow task_id=%s", task_id)
        return result

    return save_approved_workflow


__all__ = ["make_save_approved_workflow"]