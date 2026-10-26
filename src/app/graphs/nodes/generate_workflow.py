"""Generate reusable Workflow 1.0 and bind execution inputs deterministically."""

from __future__ import annotations

from app.services.async_io_resources import call_async

import asyncio

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from app.context.builder import context_payload
from app.context.budget import ContextBudgetExceeded
from agent_config import AgentSettings
from app.graphs.message_utils import as_message_content
from app.agents.orchestration.agent_interfaces import ainvoke_typed
from app.agents.orchestration.dependencies import AgentDependencies
from app.agents.workflow_generator.workflow_generator_tools import (
    _load_tool_registry_document,
    read_skill_documents,
    validate_skill_names,
)
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.graphs.nodes.candidate_contract import candidate_fingerprint
from app.services.demo_artifact_store import build_workflow_output_dir
from app.services.workflow_catalog_service import WorkflowCatalogService
from app.schemas.agents.workflow_generator_schema import (
    SkillSelectionOutput,
    WorkflowGenerationOutput,
)
from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow


MAX_WORKFLOW_GENERATION_ATTEMPTS = 3


class DuplicateGeneratedWorkflowError(ValueError):
    """The model returned an execution structure already shown or generated."""


class WorkflowGenerationDuplicatesExhausted(RuntimeError):
    """Retries produced no additional distinct candidate."""


def _workflow_validation_feedback(error: ValueError) -> str:
    feedback = "Return the complete corrected Workflow 1.0. " + str(error)
    if isinstance(error, ValidationError) and any(
        "arguments" in item["loc"] for item in error.errors()
    ):
        feedback += (
            '\nEach arguments entry must be an object with source, never a raw '
            'null, string, number, boolean, array, or empty object. '
            'For an unknown required argument use {"source":"input"} and omit '
            'its execution_inputs entry. Omit unspecified optional arguments '
            'to use Tool Registry defaults; all optional parameters and defaults '
            'will still be displayed in the input UI. For user-provided values, '
            'use {"source":"input"} and put the actual value in execution_inputs. '
            'Preserve valid tool_output references. Do not generate null as a '
            'literal value or execution_inputs value; let the user choose null '
            'in the input UI when allowed. Do not guess which value null meant.'
        )
    return feedback


def _add_missing_required_inputs(document: dict) -> None:
    """Preserve planned bindings; collect unplanned required arguments from the user."""
    registry = _load_tool_registry_document()["tools"]
    for step in document["workflow"]["steps"]:
        for tool in step["tools"]:
            for name, parameter in registry.get(tool["tool"], {}).get("inputs", {}).items():
                if (
                    not parameter.get("has_default")
                    and parameter.get("kind") in ("positional_or_keyword", "keyword_only")
                ):
                    tool["arguments"].setdefault(name, {"source": "input"})


def _has_answer(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _apply_confirmed_additional_information(
    document: dict,
    additional_information: dict,
) -> dict:
    workflow = document["workflow"]
    input_schema = workflow.setdefault("input_schema", {})
    inputs = workflow.setdefault("inputs", {})
    provenance = workflow.setdefault("input_provenance", {})
    unresolved = workflow.setdefault("unresolved_inputs", [])
    confirmed_names = set()

    for name, value in additional_information.items():
        if name not in input_schema or not _has_answer(value):
            continue
        inputs[name] = value
        provenance[name] = {
            "source": "user_answer",
            "confirmed": True,
        }
        confirmed_names.add(name)

    if confirmed_names:
        workflow["unresolved_inputs"] = [
            item
            for item in unresolved
            if item.get("name") not in confirmed_names
        ]

    remaining_unresolved = workflow.get("unresolved_inputs", [])
    if workflow.get("status") == "needs_input" and not remaining_unresolved:
        all_required_confirmed = all(
            (
                not definition.get("required", True)
                or (
                    _has_answer(inputs.get(name))
                    and provenance.get(name, {}).get("confirmed")
                )
            )
            for name, definition in input_schema.items()
        )
        if all_required_confirmed:
            workflow["status"] = "ready"

    return document


def _require_human_confirmation_for_inputs(
    document: dict,
    confirmed_information: dict,
) -> dict:
    """Discard planner-filled values for inputs that require human confirmation."""
    workflow = document["workflow"]
    input_schema = workflow.setdefault("input_schema", {})
    inputs = workflow.setdefault("inputs", {})
    provenance = workflow.setdefault("input_provenance", {})
    unresolved = workflow.setdefault("unresolved_inputs", [])
    unresolved_by_name = {
        item.get("name"): item
        for item in unresolved
        if isinstance(item, dict) and item.get("name")
    }

    for name, definition in input_schema.items():
        if (
            not definition.get("required", True)
            or definition.get("allow_llm_inference", False)
            or _has_answer(confirmed_information.get(name))
        ):
            continue
        inputs[name] = None
        provenance[name] = {"source": None, "confirmed": False}
        if name not in unresolved_by_name:
            unresolved.append(
                {
                    "name": name,
                    "question": f"{name} 값을 입력해주세요.",
                    "required_for": [],
                }
            )

    if unresolved:
        workflow["status"] = "needs_input"
    return document


def apply_missing_information_to_workflow(
    state: AnalysisWorkflowState,
) -> dict:
    if state.get("workflow_definition"):
        document = compile_workflow(
            state.get("workflow_input_definition") or state["workflow_definition"], workflow_id=state["workflow_id"],
            execution_inputs=state.get("execution_inputs", []),
        )
        document["workflow"]["context"]["output_dir"] = state.get("artifact_output_dir", "")
    else:
        document = _apply_confirmed_additional_information(
            deepcopy(state["workflow"]), state.get("additional_information", {}),
        )
    return {
        "workflow": document,
        "workflow_status": document["workflow"]["status"],
        "approval": {},
        "messages": [
            {
                "role": "agent",
                "name": "workflow_input_resolver",
                "content": ("입력값을 다시 확인해주세요." if state.get("input_errors")
                            else "입력값을 반영했습니다. 실행 계획을 확인해주세요."),
            }
        ],
    }


async def _add_current_workflow_candidate(
    state: AnalysisWorkflowState, catalog_service: WorkflowCatalogService | None = None,
    *, candidate_id: str | None = None,
    candidate_index: int = 1,
) -> dict:
    candidates = deepcopy(state.get("workflow_candidates", []))
    origin = state["workflow_origin"]
    origin_count = sum(
        1 for candidate in candidates if candidate.get("origin") == origin
    )
    workflow = deepcopy(state.get("workflow_definition") or state["workflow"])
    workflow_id = state.get("workflow_id")
    version_id = None
    content_revision = int(state.get("workflow_revision", 1))
    updates = {}
    if catalog_service is not None:
        registered = await catalog_service.register(
            workflow=workflow, registration_type="AGENT", created_by=state["user_id"],
            project_id=state.get("project_id"), session_id=state["session_id"], task_id=state["task_id"],
            idempotency_key=f"agent:workflow-register:{state['task_id']}:{state.get('workflow_revision', 1)}:{candidate_index}",
        )
        workflow_id, version_id = str(registered["workflow_id"]), str(registered["version_id"])
        content_revision = registered["revision"]
        compiled_workflow = await call_async(compile_workflow, workflow, workflow_id=workflow_id,
                                            execution_inputs=state.get("execution_inputs", []))
        compiled_workflow["workflow"]["context"]["output_dir"] = state.get("artifact_output_dir", "")
        updates = {"workflow_id":workflow_id, "workflow_version_id":version_id,
                   "workflow_content_revision":content_revision, "workflow":compiled_workflow}
    candidate_number = (state.get("workflow_revision", 1)
                        if state.get("candidate_selection_payload_version") == 2 else origin_count + 1)
    candidate = {
        "id": candidate_id or f"{origin}_{candidate_number}",
        "origin": origin,
        "status": state.get("workflow_status") or workflow["workflow"].get("status", "needs_input"),
        "workflow_id": workflow_id,
        "version_id": version_id,
        "revision": content_revision,
        "workflow": workflow,
        "execution_inputs": deepcopy(state.get("execution_inputs", [])),
    }
    if origin == "generated" and state.get("workflow_generation_notice"):
        candidate["reason"] = state["workflow_generation_notice"]
    candidates.append(candidate)
    return {
        **updates,
        "workflow_candidates": candidates,
        "messages": [
            {
                "role": "agent",
                "name": "workflow_candidate_collector",
                "content": as_message_content(
                    {
                        "status": "candidate_added",
                        "candidate_id": candidate["id"],
                        "origin": origin,
                        "workflow_status": candidate["status"],
                    }
                ),
            }
        ],
    }


async def add_current_workflow_candidate(state: AnalysisWorkflowState) -> dict:
    """Compatibility node for callers that do not configure durable storage."""

    return await _add_current_workflow_candidate(state)


def make_add_current_workflow_candidate(catalog_service: WorkflowCatalogService | None = None):
    async def add_candidate(state: AnalysisWorkflowState) -> dict:
        drafts = state.get("generated_workflow_drafts")
        if not drafts:
            return await _add_current_workflow_candidate(state, catalog_service)
        working = dict(state)
        updates = {}
        messages = []
        for index, draft in enumerate(drafts, 1):
            working.update(draft)
            candidate_id = f"generated_{draft['workflow_revision']}"
            if len(drafts) > 1:
                candidate_id += f"_{index}"
            added = await _add_current_workflow_candidate(
                working, catalog_service, candidate_id=candidate_id, candidate_index=index,
            )
            messages.extend(added.pop("messages", []))
            working.update(added)
            updates.update(added)
        return {**updates, "generated_workflow_drafts": [], "messages": messages}

    return add_candidate


def make_generate_workflow(deps: AgentDependencies, settings: AgentSettings):
    """Generate reusable 1.0 definitions before asking for any execution inputs."""
    async def generate_one(state: AnalysisWorkflowState, candidate_index: int) -> dict:
        revision = state.get("workflow_revision", 0) + 1
        if revision > settings.max_workflow_revisions:
            raise RuntimeError("maximum workflow revision count exceeded")
        workflow_id = str(uuid5(NAMESPACE_URL, f"dtest-agent/definition/{state['task_id']}/{revision}/{candidate_index}"))
        payload = {
            "user_request": state["user_request"],
            "analysis_intent": state["analysis_intent"]["intent"],
            "actor": {"type": "AGENT", "id": "workflow_generator"},
            "rejection_feedback": state.get("approval_feedback"),
            "previous_workflow": state.get("previous_workflow_definition"),
            "candidate_index": candidate_index,
            "candidate_count": settings.workflow_generation_limit,
            "generation_instruction": (
                "Return exactly one Workflow 1.0 candidate for this call. "
                "Preserve the user's goal and use only the supplied registered Skills/Tools. "
                "For subsequent candidates, choose a meaningfully different valid analysis approach "
                "or applicable Tool configuration from excluded_workflows. Do not add redundant "
                "Tool calls or change only names/actors to create artificial differences."
            ),
        }
        exclusions = state.get("excluded_workflow_candidates", [])
        if exclusions:
            payload["excluded_workflows"] = [deepcopy(item["workflow"]) for item in exclusions]
        if deps.skill_selector_agent is not None:
            selection = await ainvoke_typed(deps.skill_selector_agent, context_payload(deps, state, "skill_selection", payload), SkillSelectionOutput)
            names = await asyncio.to_thread(validate_skill_names, selection.skill_names)
            requirements = [item.model_dump(mode="json") for item in selection.dataset_requirements]
        else:
            from app.agents.workflow_generator.workflow_generator_tools import _load_skill_index_document
            names = list((await asyncio.to_thread(_load_skill_index_document))["skills"])
            requirements = []
        payload["dataset_requirements"] = requirements
        payload["selected_skill_names"] = names
        payload["workflow_resources"] = await read_skill_documents.ainvoke({"skill_names": names})
        last_error = None
        for _ in range(MAX_WORKFLOW_GENERATION_ATTEMPTS):
            try:
                generated = await ainvoke_typed(deps.workflow_agent, context_payload(deps, state, "workflow_generation", payload), WorkflowGenerationOutput)
                definition = generated.model_dump(mode="json", exclude={"execution_inputs"})
                execution_inputs = [
                    {**binding.model_dump(mode="json"), "origin": "user_request", "confirmed": False}
                    for binding in generated.execution_inputs
                ]
                definition["workflow"]["actor"] = payload["actor"]
                await asyncio.to_thread(_add_missing_required_inputs, definition)
                if candidate_fingerprint(definition) in {item["fingerprint"] for item in exclusions}:
                    payload["previous_rejected_workflow"] = deepcopy(definition)
                    raise DuplicateGeneratedWorkflowError(
                        "Generated workflow repeats an excluded candidate; generate a different execution structure")
                compiled = await asyncio.to_thread(compile_workflow, definition, workflow_id=workflow_id,
                                            execution_inputs=execution_inputs)
                break
            except ContextBudgetExceeded:
                raise
            except (ValidationError, ValueError) as exc:
                last_error = exc
                payload["validation_feedback"] = _workflow_validation_feedback(exc)
        else:
            if isinstance(last_error, DuplicateGeneratedWorkflowError):
                raise WorkflowGenerationDuplicatesExhausted(
                    "Generated workflow repeats an excluded candidate after retries; "
                    "no additional distinct workflow") from last_error
            raise RuntimeError(f"workflow generation failed validation after retries: {last_error}") from last_error
        return {
            "workflow_definition": definition,
            "workflow_id": workflow_id,
            "workflow": compiled,
            "workflow_status": compiled["workflow"]["status"],
            "workflow_origin": "generated",
            "workflow_revision": revision,
            "execution_inputs": execution_inputs,
            "artifact_output_dir": await asyncio.to_thread(build_workflow_output_dir, settings, state),
            "approval": {},
            "messages": [{"role": "agent", "name": "workflow_generator",
                          "id": f"workflow-{workflow_id}",
                          "content": "요청에 맞는 워크플로우 후보를 생성했습니다. 후보를 확인하고 선택해 주세요."}],
        }
    async def workflow_node(state: AnalysisWorkflowState) -> dict:
        drafts = []
        notice = None
        exclusions = deepcopy(state.get("excluded_workflow_candidates", []))
        for index in range(1, settings.workflow_generation_limit + 1):
            try:
                draft = await generate_one({**state, "excluded_workflow_candidates": exclusions}, index)
            except WorkflowGenerationDuplicatesExhausted:
                if not drafts:
                    raise
                notice = (f"요청한 {settings.workflow_generation_limit}개 중 서로 다른 워크플로우 후보 "
                          f"{len(drafts)}개를 생성했습니다. 추가 생성 결과가 기존 후보와 중복되어 "
                          "생성된 후보만 표시합니다.")
                break
            drafts.append(draft)
            exclusions.append({"fingerprint": candidate_fingerprint(draft["workflow_definition"]),
                               "workflow": deepcopy(draft["workflow_definition"])})
        result = dict(drafts[-1])
        result["generated_workflow_drafts"] = drafts
        result["workflow_generation_notice"] = notice
        result["messages"] = [{"role": "agent", "name": "workflow_generator",
                               "id": f"workflow-{result['workflow_id']}",
                               "content": notice or f"요청에 맞는 워크플로우 후보 {len(drafts)}개를 생성했습니다. 후보를 확인하고 선택해 주세요."}]
        return result

    return workflow_node
