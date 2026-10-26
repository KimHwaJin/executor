"""Read Executor Step results from event-referenced shared-PV manifests."""

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from agent_config import AgentSettings
from app.schemas.executor_step_manifest import StepResultManifest


def _safe_resolve(root: Path, relative_path: str, *, base: Path | None = None) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"unsafe shared-PV relative path: {relative_path!r}")
    resolved_root = root.resolve()
    resolved = ((base or resolved_root) / Path(*relative.parts)).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"shared-PV path escapes configured root: {relative_path!r}"
        ) from exc
    return resolved


def _verified_bytes(
    path: Path,
    *,
    expected_size: int | None,
    expected_sha256: str,
) -> bytes:
    data = path.read_bytes()
    if expected_size is not None and len(data) != expected_size:
        raise ValueError(
            f"result file size mismatch for {path}: "
            f"expected {expected_size}, got {len(data)}"
        )
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(f"result file checksum mismatch for {path}")
    return data


def _output_type(kind: str) -> str:
    return {
        "STREAM": "stream",
        "DISPLAY": "display_data",
        "RESULT": "execute_result",
        "ERROR": "error",
    }[kind]


def _read_outputs(
    root: Path,
    manifest_path: Path,
    manifest: StepResultManifest,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for manifest_output in manifest.outputs:
        output: dict[str, Any] = {
            "output_type": _output_type(manifest_output.kind),
        }
        if manifest_output.stream_name is not None:
            output["name"] = manifest_output.stream_name
        if manifest_output.execution_count is not None:
            output["execution_count"] = manifest_output.execution_count

        representations: list[dict[str, Any]] = []
        data_by_mime: dict[str, Any] = {}
        for representation in manifest_output.representations:
            # Executor manifest representation paths use the same shared-PV
            # root-relative contract as result_ref.relative_path.  Resolving
            # them from manifest_path.parent duplicates the execution path.
            output_path = _safe_resolve(
                root,
                representation.relative_path,
            )
            raw = _verified_bytes(
                output_path,
                expected_size=representation.size_bytes,
                expected_sha256=representation.checksum_sha256,
            )
            reference = {
                "media_type": representation.media_type,
                "path": str(output_path),
                "size_bytes": representation.size_bytes,
                "checksum_sha256": representation.checksum_sha256,
            }
            representations.append(reference)
            if representation.encoding == "UTF8":
                content: Any = raw.decode("utf-8")
                if representation.media_type == "application/json":
                    try:
                        content = json.loads(content)
                    except json.JSONDecodeError:
                        pass
                data_by_mime[representation.media_type] = content

        output["representations"] = representations
        if manifest_output.kind == "STREAM":
            text = data_by_mime.get("text/plain")
            if isinstance(text, str):
                output["text"] = text
        else:
            output["data"] = data_by_mime
        outputs.append(output)
    return outputs


def _operation_payload(state: dict[str, Any]) -> dict[str, Any]:
    execution_event = state.get("execution_event")
    if not isinstance(execution_event, dict):
        raise RuntimeError("manifest result collection requires execution_event")
    payload = execution_event.get("response")
    if not isinstance(payload, dict):
        raise RuntimeError("Executor event does not contain an operation payload")
    operation = payload.get("operation")
    expected_number = int(state.get("executor_operation_number", 1))
    if not isinstance(operation, dict) or int(operation.get("number", 0)) != expected_number:
        raise RuntimeError(
            f"Executor event is missing operation {expected_number}"
        )
    return payload


def read_current_operation_results_from_manifest(
    settings: AgentSettings,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the existing normalized Tool results without Result/Notebook GETs."""

    execution_id = str(state.get("execution_id") or "")
    if not execution_id:
        raise RuntimeError("manifest result collection requires execution_id")
    payload = _operation_payload(state)
    step_results = payload.get("step_results")
    if not isinstance(step_results, list):
        raise RuntimeError("Executor operation event does not contain step_results")

    steps_by_sequence = {
        int(step["sequence"]): step
        for step in state.get("execution_steps") or []
        if isinstance(step, dict) and step.get("sequence") is not None
    }
    root = settings.executor_shared_result_root
    results: list[dict[str, Any]] = []
    for event_step in sorted(step_results, key=lambda item: int(item["sequence"])):
        sequence = int(event_step["sequence"])
        step = steps_by_sequence.get(sequence) or {}
        lineage = step.get("lineage") or {}
        inputs = lineage.get("input_parameters") or {}
        tool_id = str(inputs.get("tool_id") or "")
        if not tool_id:
            continue

        executor_status = str(event_step.get("status") or "PENDING")
        status = (
            "NOT_EXECUTED"
            if executor_status in {"PENDING", "SKIPPED"}
            else executor_status
        )
        result_ref = event_step.get("result_ref")
        manifest: StepResultManifest | None = None
        outputs: list[dict[str, Any]] = []
        if isinstance(result_ref, dict):
            if result_ref.get("storage") != "SHARED_PV":
                raise ValueError("manifest result_ref.storage must be SHARED_PV")
            manifest_path = _safe_resolve(root, str(result_ref["relative_path"]))
            manifest_bytes = _verified_bytes(
                manifest_path,
                # Result API references provide a checksum but may omit manifest byte size.
                expected_size=int(result_ref["size_bytes"]) if "size_bytes" in result_ref else None,
                expected_sha256=str(result_ref["checksum_sha256"]),
            )
            manifest = StepResultManifest.model_validate_json(manifest_bytes)
            identity = manifest.identity
            if str(identity.execution_id) != execution_id:
                raise ValueError("manifest execution_id does not match event")
            if str(identity.operation_id) != str(payload["operation"]["id"]):
                raise ValueError("manifest operation_id does not match event")
            if str(identity.step_id) != str(event_step.get("step_id") or ""):
                raise ValueError("manifest step_id does not match event")
            if identity.sequence != sequence:
                raise ValueError("manifest sequence does not match event")
            attempt = event_step.get("attempt") or {}
            if str(identity.execution_attempt_id) != str(attempt.get("id") or ""):
                raise ValueError("manifest execution_attempt_id does not match event")
            ref_fencing_token = result_ref.get("fencing_token")
            if (
                ref_fencing_token is not None
                and identity.fencing_token != int(ref_fencing_token)
            ):
                raise ValueError("manifest fencing_token does not match result_ref")
            outputs = _read_outputs(root, manifest_path, manifest)
        elif status not in {"NOT_EXECUTED", "FAILED", "CANCELLED"}:
            raise RuntimeError(
                f"Executor Step {event_step.get('step_id')} has no result_ref"
            )

        is_workflow_output = (
            lineage.get("skill_name") == "workflow"
            or lineage.get("tool_name") == "workflow_outputs"
        )
        results.append(
            {
                "tool_id": tool_id,
                "role": "workflow_outputs" if is_workflow_output else "tool_execution",
                "result": {
                    "status": status,
                    "executor_status": executor_status,
                    "step_id": inputs.get("step_id"),
                    "executor_step_id": event_step.get("step_id"),
                    "cell_index": sequence,
                    "execution_count": manifest.execution_count if manifest else None,
                    "error_message": (
                        (manifest.error_message if manifest else None)
                        or event_step.get("error_message")
                        or (event_step.get("error") or {}).get("message")
                        or ((payload.get("error") or {}).get("message")
                            if status in {"FAILED", "CANCELLED"} else None)
                    ),
                    "output_summary": (
                        manifest.output_summary.model_dump(mode="json")
                        if manifest
                        else event_step.get("output_summary") or {}
                    ),
                    "result_ref": result_ref,
                    "outputs": outputs,
                },
            }
        )
    return results


async def read_terminal_results_from_manifest(settings, state, *, fetch_result):
    """execution.completed has no operation/Step refs; resolve them via Result API.

    Raw outputs still come from the shared PV, never from Notebook GET.
    """
    import asyncio
    from app.services.async_io_resources import call_async

    response = await call_async(fetch_result, settings, state["execution_id"])
    body = response.get("body") or {}
    number = int(state.get("executor_operation_number", 1))
    operation = next((item for item in body.get("operations", [])
                      if int(item.get("operation_number", 0)) == number), None)
    event = state.get("execution_event") or {}
    if operation is None:
        # Runtime preparation can fail before any Operation result exists.
        if not body.get("operations") and event.get("status") in {"FAILED", "CANCELLED"}:
            return []
        raise RuntimeError(f"Executor result is missing operation {number}")
    event_steps = []
    for step in operation.get("steps") or []:
        result = step.get("result") or {}
        ref = result.get("result_ref")
        event_steps.append({
            "step_id": step["step_id"], "sequence": step["sequence"],
            "status": result.get("status", "PENDING"), "result_ref": ref,
            "attempt": {"id": (ref or {}).get("attempt_id")},
            "output_summary": result.get("output_summary"),
            "error_message": result.get("error_message"),
        })
    payload = {
        "operation": {"id": operation["operation_id"], "number": number},
        "step_results": event_steps,
        "error": (event.get("response") or {}).get("error"),
    }
    # Keep submitted lineage when the Result API omits it.
    steps = {s["sequence"]: s for s in state.get("execution_steps", [])}
    for step in operation.get("steps") or []:
        if step.get("lineage"):
            steps[step["sequence"]] = step
    return await asyncio.to_thread(read_current_operation_results_from_manifest, settings, {
        **state, "execution_event": {**event, "response": payload},
        "execution_steps": list(steps.values()),
    })


__all__ = ["read_current_operation_results_from_manifest", "read_terminal_results_from_manifest"]
