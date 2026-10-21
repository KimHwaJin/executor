"""Deterministic naming, paths, and identities for materialized Artifacts."""

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import PurePosixPath
from uuid import UUID

from executor_service.application.commands import MaterializeArtifactCommand
from executor_service.domain.enums import ArtifactType
from executor_service.domain.errors import ArtifactRegistrationError

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
ARTIFACT_DIRECTORIES = {
    ArtifactType.DATASET: "datasets",
    ArtifactType.PLOT: "plots",
    ArtifactType.MODEL: "models",
    ArtifactType.METRIC: "metrics",
    ArtifactType.LOG: "logs",
    ArtifactType.OTHER: "other",
}


def command_fingerprint(command: MaterializeArtifactCommand) -> str:
    payload = asdict(command)
    payload.pop("idempotency_key")
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def artifact_id(fingerprint: str) -> UUID:
    return UUID(bytes=hashlib.sha256(fingerprint.encode()).digest()[:16])


def artifact_identity_hash(
    execution_id: UUID, target_path: str, checksum_sha256: str
) -> str:
    return hashlib.sha256(
        f"{execution_id}:{target_path}:{checksum_sha256}".encode()
    ).hexdigest()


def artifact_name(command: MaterializeArtifactCommand) -> str:
    default_name = (
        "final-report.md"
        if command.artifact_type == ArtifactType.REPORT
        else "artifact.txt"
    )
    name = command.name or default_name
    if not SAFE_NAME.fullmatch(name):
        raise ArtifactRegistrationError(
            "Artifact name contains unsafe characters."
        )
    if (
        command.artifact_type == ArtifactType.REPORT
        and not name.lower().endswith(".md")
    ):
        name += ".md"
    return name


def target_path(workspace: str, artifact_type: ArtifactType, name: str) -> str:
    root = PurePosixPath(workspace)
    if artifact_type == ArtifactType.REPORT:
        return (root / "reports" / name).as_posix()
    directory = ARTIFACT_DIRECTORIES.get(artifact_type, "other")
    return (root / "artifacts" / directory / name).as_posix()


def media_type(
    command: MaterializeArtifactCommand, runtime_media_type: str | None
) -> str:
    if command.media_type is not None:
        return command.media_type
    if command.artifact_type == ArtifactType.REPORT:
        return "text/markdown"
    return runtime_media_type or "text/plain"
