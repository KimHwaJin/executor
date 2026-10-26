"""API-only validation against immutable, persisted V2 display data.

No default application, compilation, graph routing or task cancellation occurs here.
"""
from fastapi import HTTPException
from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError
from pydantic import ValidationError

from app.schemas.common.workflow_output_catalog import output_reference_allowed
from app.schemas.common.workflow_hitl_schema import (
    CANDIDATE_SUBMISSION, ReasonRejection, ToolInputSubmission,
)


def invalid(message, path=()):
    raise HTTPException(422, {"code": "INVALID_INTERACTION_INPUT",
                              "path": list(path), "message": message})


def validate_v2(action, payload):
    name, display = action["name"], action.get("args", {})
    # Only well-typed, supplied revisions are stale; missing/invalid ones are 422.
    if name == "additional_information":
        revision = payload.get("workflow_revision")
        if type(revision) is int and revision >= 1 and revision != display.get("workflow_revision"):
            raise HTTPException(409, "Workflow revision is stale.")
    try:
        if name == "workflow_candidate_selection":
            submitted = CANDIDATE_SUBMISSION.validate_python(payload)
        elif name == "workflow_approval":
            submitted = ReasonRejection.model_validate(payload)
        elif name == "additional_information":
            submitted = ToolInputSubmission.model_validate(payload)
        else:
            invalid("Unsupported v2 interaction.")
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        invalid(error["msg"], error["loc"])

    allowed = {item["intent"] for item in display.get("available_actions", [])}
    if submitted.intent not in allowed:
        invalid("Intent is not available for this interaction.", ["intent"])
    if name == "workflow_candidate_selection" and submitted.intent == "select":
        if submitted.candidate_id not in {c["candidate_id"] for c in display.get("candidates", [])}:
            invalid("Candidate is not in the stored candidate list.", ["candidate_id"])
    if name != "additional_information":
        return
    if submitted.workflow_id != display.get("workflow_id"):
        invalid("Workflow does not match the stored input target.", ["workflow_id"])
    parameters = {
        (tool["step_index"], tool["tool_index"], parameter["name"]): parameter
        for tool in display.get("tool_inputs", []) for parameter in tool["parameters"]
    }
    seen = set()
    for index, item in enumerate(submitted.inputs):
        address = (item.step_index, item.tool_index, item.parameter)
        path = ["inputs", index]
        if address in seen:
            invalid("Duplicate tool input address.", path)
        seen.add(address)
        parameter = parameters.get(address)
        if parameter is None:
            invalid("Unknown tool input address.", path)
        if not parameter.get("editable"):
            raise HTTPException(422, {"code": "READ_ONLY_INPUT", "path": path,
                                      "field": item.parameter})
        if item.source == "default":
            if not parameter.get("has_default"):
                invalid("Parameter has no default.", path)
            continue
        if item.source == "tool_output":
            reference = item.reference.model_dump()
            if not output_reference_allowed(display, address, reference):
                invalid("Output reference is not offered for this parameter.", path + ["reference"])
            continue
        try:
            Draft202012Validator(parameter["value_schema"]).validate(item.value)
        except SchemaValidationError as exc:
            invalid(exc.message, path + ["value"] + list(exc.absolute_path))
    for address, parameter in parameters.items():
        if parameter.get("editable") and parameter.get("required") and not parameter.get("binding") and address not in seen:
            invalid("Required tool input is missing: %s" % (address,), ["inputs"])
