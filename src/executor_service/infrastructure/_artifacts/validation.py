"""Artifact path classification, validation, and metadata redaction."""

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from executor_service.domain.enums import ArtifactType
from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.runtime import RuntimeFileState
from executor_service.infrastructure.workspace import ExecutionWorkspace

ARTIFACT_DIRECTORY_TYPES = {
    "datasets": ArtifactType.DATASET,
    "plots": ArtifactType.PLOT,
    "models": ArtifactType.MODEL,
    "metrics": ArtifactType.METRIC,
    "reports": ArtifactType.REPORT,
    "logs": ArtifactType.LOG,
    "other": ArtifactType.OTHER,
}


def infer_artifact_type(
    state: RuntimeFileState, workspace: ExecutionWorkspace
) -> ArtifactType | None:
    path = PurePosixPath(state.path)
    root = PurePosixPath(workspace.artifacts_path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) <= 1:
        return None
    return ARTIFACT_DIRECTORY_TYPES.get(relative.parts[0])


def manifest_runtime_path(raw: str, workspace: ExecutionWorkspace) -> str:
    path = PurePosixPath(raw)
    if path.is_absolute() or (path.parts and path.parts[0] == "users"):
        return raw
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactRegistrationError(
            "PV manifest path contains an unsafe segment."
        )
    return f"{workspace.runtime_relative_path}/{path.as_posix()}"


def validate_s3_uri(uri: str) -> None:
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or not parsed.path.lstrip("/")
    ):
        raise ArtifactRegistrationError(
            "S3 Artifact uri must be s3://bucket/key."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ArtifactRegistrationError(
            "S3 Artifact uri must not contain credentials or query."
        )


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if is_secret_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def is_secret_key(key: str) -> bool:
    normalized = key.lower()
    return any(
        marker in normalized
        for marker in ("token", "secret", "password", "credential")
    )
