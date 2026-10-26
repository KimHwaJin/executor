"""Workflow API coordination and stable, replayable UI state."""
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError
from sqlalchemy import select

from app.core.enums import AgentRunStatus, DeleteYN
from app.models import AgentRunModel, SessionModel, UserModel
from app.repositories.project_repository import ProjectRepository
from app.repositories.session_repository import SessionRepository
from app.schemas.common.message_schema import MessageCreate
from app.schemas.common.run_schema import RunCreate, RunInput, RunResource
from app.services.message_service import MessageService
from app.services.run_service import RunService
from app.services.session_service import SessionService
from app.services.workflow_interactions import interactions
from app.services.workflow_hitl_validation import validate_v2


TERMINAL = {AgentRunStatus.SUCCESS, AgentRunStatus.ERROR, AgentRunStatus.TIMEOUT, AgentRunStatus.CANCELED}


def workflow_status(run):
    waiting = run.status == AgentRunStatus.INTERRUPTED
    return {
        "run_id": str(run.run_id),
        "task_id": str(run.task_id) if run.task_id else None,
        "session_id": str(run.session_id),
        "status": "waiting_input" if waiting else run.status.value,
        "run_status": run.status.value,
        "stream_finished": waiting or run.status in TERMINAL,
        "run_finished": run.status in TERMINAL,
        "input_required": waiting,
        "interactions": interactions(run) if waiting else [],
        "failure": run.failure,
    }


def workflow_resource(run):
    return {**RunResource.model_validate(run).model_dump(mode="json"), **workflow_status(run)}


def resume_command(run, resume):
    current = next((item for item in interactions(run)
                    if item["interaction_id"] == str(resume.interaction_id)), None)
    if current is None or current["revision"] != resume.revision:
        raise HTTPException(409, "Interaction or revision is stale.")
    actions = current.get("action_requests", [])
    configs = current.get("review_configs", [])
    if len(actions) != 1 or len(configs) != 1:
        raise HTTPException(409, "This interaction does not support workflow_run resume.")
    action = actions[0]
    version = current.get("payload_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise HTTPException(409, "Unsupported interaction payload version.")
    allowed = configs[0].get("allowed_decisions", [])
    decision = resume.action
    # The domain action name means submitting the form/answer for that action.
    if version == 1 and decision == action["name"]:
        decision = "edit" if "edit" in allowed else "respond"
    if version == 2:
        supported = {"approve", "edit"} if action["name"] == "workflow_approval" else {"edit"}
        if decision not in supported:
            raise HTTPException(422, "V2 requires edit payload or plan approval.")
    if decision not in allowed:
        raise HTTPException(422, "Action is not allowed for this interaction.")
    if decision == "approve":
        if version == 2 and "approve" not in {
            item["intent"] for item in action.get("args", {}).get("available_actions", [])
        }:
            raise HTTPException(422, "Approval is not available for this interaction.")
        if resume.payload is not None:
            raise HTTPException(422, "Use edit to change approved arguments.")
        value = {"type": "approve"}
    elif decision == "reject":
        value = {"type": "reject", "message": resume.feedback}
    elif decision == "edit":
        args = resume.payload
        if args is None and version == 2:
            raise HTTPException(422, "V2 edit requires a structured payload.")
        if args is None:
            try:
                args = json.loads(resume.feedback)
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, "edit requires payload or JSON object feedback.") from exc
        if not isinstance(args, dict):
            raise HTTPException(422, "Edited arguments must be an object.")
        if version == 2:
            validate_v2(action, args)
        schema = configs[0].get("args_schema", {})
        try:
            Draft202012Validator(schema).validate(args)
        except SchemaValidationError as exc:
            raise HTTPException(422, {"code": "INVALID_INTERACTION_INPUT",
                                      "path": list(exc.absolute_path), "message": exc.message}) from exc
        for name, definition in (schema.get("properties", {}) if version == 1 else {}).items():
            if definition.get("readOnly") and args.get(name) != action.get("args", {}).get(name):
                raise HTTPException(422, {"code": "READ_ONLY_INPUT", "field": name})
        value = {"type": "edit", "edited_action": {"name": action["name"], "args": args}}
    else:
        value = {"type": "respond", "message": resume.payload if resume.payload is not None else resume.feedback}
    return {"decisions": [value]}


async def owned_run(db, user_id, run_id):
    run = await db.scalar(select(AgentRunModel).join(
        SessionModel, SessionModel.session_id == AgentRunModel.session_id,
    ).where(
        AgentRunModel.run_id == run_id,
        SessionModel.user_id == user_id,
        SessionModel.delete_yn == DeleteYN.N,
    ))
    if run is None:
        raise HTTPException(404, "Run not found.")
    return run


async def submit_workflow(payload, key, user_id, db, *, metadata_extra=None):
    if not key or not key.strip() or len(key) > 255:
        raise HTTPException(422, "Idempotency-Key must contain 1–255 nonblank characters.")
    fingerprint = hashlib.sha256(json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    run_key = "workflow_run:" + hashlib.sha256(key.encode()).hexdigest()
    # User-scoped serialization also protects retries which omit session_id.
    await db.scalar(select(UserModel).where(UserModel.user_id == user_id).with_for_update())
    previous = await db.scalar(select(AgentRunModel).join(
        SessionModel, SessionModel.session_id == AgentRunModel.session_id,
    ).where(SessionModel.user_id == user_id, AgentRunModel.idempotency_key == run_key))
    if previous is not None:
        if (previous.metadata_json or {}).get("workflow_fingerprint") != fingerprint:
            raise HTTPException(409, "Idempotency-Key was used with a different request body.")
        if await SessionRepository.get_active_by_user(db, user_id=user_id, session_id=previous.session_id) is None:
            raise HTTPException(404, "Session not found.")
        return previous
    metadata = {**(metadata_extra or {}), "workflow_fingerprint": fingerprint}
    if payload.command is not None:
        origin = await owned_run(db, user_id, payload.run_id)
        session_id = origin.session_id
        await SessionRepository.get_active_by_user(db, user_id=user_id, session_id=session_id, for_update=True)
        latest = await db.scalar(select(AgentRunModel).where(
            AgentRunModel.task_id == payload.task_id,
        ).order_by(AgentRunModel.created_at.desc(), AgentRunModel.run_id.desc()).limit(1))
        if (origin.task_id != payload.task_id or (latest is None or latest.run_id != origin.run_id)
                or origin.status != AgentRunStatus.INTERRUPTED):
            raise HTTPException(409, "Run is stale or task is not waiting for input.")
        command = resume_command(origin, payload.command.resume)
        metadata["resume_run_id"] = str(origin.run_id)
        metadata["workflow_resume"] = payload.command.resume.model_dump(mode="json")
        run_payload = RunCreate(command=command, metadata=metadata)
    else:
        session_id = payload.session_id
        if session_id is None:
            project_id = payload.project_id
            if project_id is None:
                project = await ProjectRepository.get_default(db, user_id=user_id)
                if project is None:
                    raise HTTPException(409, "A default project is required.")
                project_id = project.project_id
            session = await SessionService.create_internal(db, user_id=user_id, project_id=project_id)
            session_id = session.session_id
        saved = await MessageService.create(db, user_id, MessageCreate(
            session_id=session_id, project_id=payload.project_id,
            content_text="\n".join(part.text for part in payload.input.content),
            content=[part.model_dump() for part in payload.input.content],
            client_request_id=uuid5(NAMESPACE_URL, f"workflow_run:{user_id}:{key}"),
        ), commit=False)
        metadata["trigger_message_id"] = str(saved.message.message_id)
        run_payload = RunCreate(input=RunInput(messages=[{
            "role": "user", "content": saved.message.content_text,
        }]), metadata=metadata)
    return await RunService.create(db, user_id, session_id, run_payload, run_key)
