"""Rule-based approved-Workflow notebook code generation node."""

from __future__ import annotations

import logging

from agent_config import AgentSettings
from app.graphs.message_utils import as_message_content
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.services.demo_artifact_store import build_run_artifact_dir
from app.services.rule_based_notebook_generator import (
    APP_SOURCE_ROOT,
    build_notebook,
    write_cell_code_files,
)
from app.agents.workflow_generator.data_load_steps import (
    validate_target_data_role_lineage,
)
from app.schemas.agents.orchestration_schema import NotebookGenerationOutput

logger = logging.getLogger(__name__)


def make_build_notebook_code(settings: AgentSettings):
    logger.debug(
        "[DEBUG] ENTER make_build_notebook_code (graph setup)",
    )

    def build_notebook_code(state: AnalysisWorkflowState) -> dict:
        task_id = state.get("task_id")
        logger.debug(
            "[DEBUG] ENTER build_notebook_code task_id=%s demo_artifacts_enabled=%s",
            task_id, settings.demo_artifacts_enabled,
        )
        logger.debug("[DEBUG] Notebook lineage validation start task_id=%s", task_id)
        if state.get("workflow_definition"):
            if not state.get("approval", {}).get("approved") or state["workflow_status"] != "ready":
                raise ValueError("Notebook generation requires a ready, approved Workflow")
        else:
            validate_target_data_role_lineage(state["workflow"], state["data_selection"])
        logger.debug("[DEBUG] Notebook lineage validation complete task_id=%s", task_id)
        logger.debug(
            "[DEBUG] Notebook generation start task_id=%s project_root=%s",
            task_id, APP_SOURCE_ROOT,
        )
        artifact = build_notebook(
            state["workflow"],
            project_root=APP_SOURCE_ROOT,
            job_id=state["task_id"],
        )
        logger.debug(
            "[DEBUG] Notebook generation complete task_id=%s cells=%s",
            task_id, len(artifact["cells"]),
        )
        if logger.isEnabledFor(logging.DEBUG):
            for cell in artifact["cells"]:
                logger.debug(
                    "[DEBUG] Generated notebook cell task_id=%s cell_id=%s step_id=%s tool_id=%s code_chars=%s",
                    task_id, cell.get("id"), cell.get("step_id"),
                    cell.get("tool_id"), len(cell.get("code") or ""),
                )
        logger.debug("[DEBUG] Notebook output validation start task_id=%s", task_id)
        output = NotebookGenerationOutput(
            message="승인된 계획의 실행 코드를 준비했습니다.",
            cells=[
                {
                    "order": cell["order"],
                    "id": cell["id"],
                    "cell_type": cell["cell_type"],
                    "role": cell["role"],
                    "skill": cell["skill"],
                    "code": cell["code"],
                    "step_id": cell["step_id"],
                    "tool_id": cell["tool_id"],
                    "tool": cell["tool"],
                }
                for cell in artifact["cells"]
            ],
        )
        logger.debug("[DEBUG] Notebook output validation complete task_id=%s", task_id)
        logger.debug("[DEBUG] Notebook payload serialization start task_id=%s", task_id)
        payload = output.model_dump(mode="json")
        logger.debug("[DEBUG] Notebook payload serialization complete task_id=%s", task_id)
        artifact_files = dict(state.get("artifact_files", {}))
        if settings.demo_artifacts_enabled:
            logger.debug("[DEBUG] Notebook demo directory prepare start task_id=%s", task_id)
            run_dir = build_run_artifact_dir(
                settings,
                user_id=state["user_id"],
                project_id=state["project_id"],
                session_id=state["session_id"],
                task_id=state["task_id"],
            )
            logger.debug(
                "[DEBUG] Notebook demo directory prepare complete task_id=%s absolute_path=%s is_dir=%s",
                task_id, run_dir.resolve(), run_dir.is_dir(),
            )
            logger.debug(
                "[DEBUG] Notebook demo cell files write start task_id=%s absolute_path=%s",
                task_id, (run_dir / "cells").resolve(),
            )
            paths = write_cell_code_files(artifact, run_dir / "cells")
            artifact_files["cells"] = [str(path) for path in paths]
            logger.debug(
                "[DEBUG] Notebook demo cell files write complete task_id=%s files=%s",
                task_id, len(paths),
            )
            if logger.isEnabledFor(logging.DEBUG):
                for path in paths:
                    logger.debug(
                        "[DEBUG] Notebook demo cell file task_id=%s absolute_path=%s is_file=%s",
                        task_id, path.resolve(), path.is_file(),
                    )
        else:
            logger.debug(
                "[DEBUG] Notebook demo cell files skipped task_id=%s demo_artifacts_enabled=false",
                task_id,
            )
        logger.debug("[DEBUG] Notebook state update build start task_id=%s", task_id)
        result = {
            "notebook": payload,
            "adaptive_pending_tool_id": artifact["workflow"].get(
                "pending_conditional_tool_id"
            ),
            "adaptive_generated_tool_ids": [
                cell["tool_id"]
                for cell in artifact["cells"]
                if cell.get("tool_id")
            ],
            "adaptive_runtime_decisions": {},
            "adaptive_execution_plan": {
                "workflow_id": state["workflow"]["workflow"]["id"],
                "runtime_decisions": {},
            },
            "adaptive_observations": {},
            "adaptive_executed_tool_ids": [],
            "adaptive_round": 1,
            "adaptive_status": (
                "waiting_for_execution_results"
                if artifact["workflow"].get("pending_conditional_tool_id")
                else "complete"
            ),
            "artifact_files": artifact_files,
            "messages": [
                {
                    "role": "agent",
                    "name": "rule_based_notebook_generator",
                    "content": as_message_content(
                        {
                            "message": output.message,
                            "cell_count": len(output.cells),
                        }
                    ),
                }
            ],
        }
        logger.debug(
            "[DEBUG] EXIT build_notebook_code task_id=%s cells=%s adaptive_status=%s pending_tool_id=%s",
            task_id, len(output.cells), result["adaptive_status"],
            result["adaptive_pending_tool_id"],
        )
        return result

    return build_notebook_code