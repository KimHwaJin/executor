"""MCP lifecycle queries plus safe shared-volume result resolution."""

import hashlib
import json
from pathlib import Path
from typing import Any

from mcp import Client


class ExecutorToolError(RuntimeError):
    """Raised when an Executor MCP tool returns no structured result."""


class ExecutionResultReadError(RuntimeError):
    """Raised when a shared result reference is unsafe or corrupted."""


async def required_tool_result(
    client: Client, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        raise ExecutorToolError(f"{tool} failed: {result.content}")
    return result.structured_content


async def collect_execution_result(
    client: Client,
    execution_id: str,
    shared_storage_root: Path,
) -> dict[str, Any]:
    """Reconcile DB state over MCP, then resolve output bodies from shared PV."""

    result = await required_tool_result(
        client,
        "execution_result_get",
        {"execution_id": execution_id},
    )
    for operation in result["operations"]:
        for step in operation["steps"]:
            reference = step["result"].get("result_ref")
            step["result"]["resolved_result"] = (
                read_step_result(shared_storage_root, reference) if reference is not None else None
            )
    return result


def read_step_result(
    shared_storage_root: Path,
    reference: dict[str, Any],
) -> dict[str, Any]:
    """Resolve only an Executor-issued manifest and its declared files."""

    if reference.get("storage") != "SHARED_PV":
        raise ExecutionResultReadError("Unsupported result storage type.")
    root = shared_storage_root.resolve()
    manifest_path = _resolve(root, str(reference["relative_path"]))
    manifest_body = manifest_path.read_bytes()
    if _sha256(manifest_body) != reference.get("checksum_sha256"):
        raise ExecutionResultReadError("Result manifest checksum failed.")
    try:
        manifest = json.loads(manifest_body)
    except json.JSONDecodeError as exc:
        raise ExecutionResultReadError("Result manifest is invalid JSON.") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
        raise ExecutionResultReadError("Result manifest schema is unsupported.")
    identity = manifest.get("identity")
    if not isinstance(identity, dict) or (
        str(identity.get("execution_id")) != str(reference["execution_id"])
        or str(identity.get("step_id")) != str(reference["step_id"])
        or str(identity.get("execution_attempt_id")) != str(reference["attempt_id"])
        or identity.get("fencing_token") != reference["fencing_token"]
    ):
        raise ExecutionResultReadError("Result manifest identity conflicts.")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        raise ExecutionResultReadError("Result manifest outputs are invalid.")
    manifest["outputs"] = [
        _resolve_output(root, manifest_path.parent, output) for output in outputs
    ]
    return manifest


def _resolve_output(
    root: Path,
    result_directory: Path,
    output: object,
) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ExecutionResultReadError("Result output descriptor is invalid.")
    representations = output.get("representations")
    if not isinstance(representations, list):
        raise ExecutionResultReadError("Result representations are invalid.")
    resolved = dict(output)
    resolved["representations"] = [
        _resolve_representation(root, result_directory, value) for value in representations
    ]
    return resolved


def _resolve_representation(
    root: Path,
    result_directory: Path,
    value: object,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionResultReadError("Result representation is invalid.")
    path = _resolve(result_directory, str(value.get("relative_path", "")))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ExecutionResultReadError("Result content escapes shared root.") from exc
    body = path.read_bytes()
    if len(body) != value.get("size_bytes") or _sha256(body) != value.get("checksum_sha256"):
        raise ExecutionResultReadError("Result content checksum failed.")
    resolved = dict(value)
    resolved["content_path"] = path.as_posix()
    media_type = str(value.get("media_type", ""))
    if media_type.startswith("text/") or media_type in {
        "application/json",
        "application/javascript",
        "application/xml",
    }:
        try:
            resolved["content"] = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionResultReadError("Text result content is not UTF-8.") from exc
    return resolved


def _resolve(root: Path, raw_relative: str) -> Path:
    candidate = Path(raw_relative)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ExecutionResultReadError("Result path is unsafe.")
    try:
        resolved = (root / candidate).resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ExecutionResultReadError(
            "Result path is unavailable or outside shared storage."
        ) from exc
    return resolved


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
