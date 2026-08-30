"""Discover manifest-declared and workspace-diff Runtime Artifacts."""

from executor_service.domain.enums import ArtifactStatus
from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.runtime import (
    RuntimeStorage,
    RuntimeStorageSnapshot,
)
from executor_service.infrastructure._artifacts.descriptors import (
    manifest_descriptor,
    runtime_file_descriptor,
)
from executor_service.infrastructure._artifacts.models import (
    ArtifactDescriptor,
    ArtifactManifestEntry,
)
from executor_service.infrastructure._artifacts.validation import (
    infer_artifact_type,
)
from executor_service.infrastructure.workspace import ExecutionWorkspace


class ArtifactDiscovery:
    async def discover(
        self,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        before: RuntimeStorageSnapshot,
        status: ArtifactStatus,
    ) -> list[ArtifactDescriptor]:
        manifest = await driver.read_manifest(
            workspace.runtime_relative_path, before.manifest_size
        )
        manifest_descriptors = await self._manifest_descriptors(
            driver, workspace, manifest, status
        )
        manifest_uris = {descriptor.uri for descriptor in manifest_descriptors}
        current = await driver.artifact_snapshot(
            workspace.runtime_relative_path
        )
        previous = {state.path: state for state in before.files}
        automatic: list[ArtifactDescriptor] = []
        for state in current.files:
            if previous.get(state.path) == state:
                continue
            artifact_type = infer_artifact_type(state, workspace)
            if artifact_type is None:
                continue
            metadata = await driver.file_metadata(state.path)
            descriptor = runtime_file_descriptor(
                metadata,
                artifact_type,
                status,
                metadata={"discovery": "runtime-workspace-diff"},
            )
            if descriptor.uri not in manifest_uris:
                automatic.append(descriptor)
        return [*manifest_descriptors, *automatic]

    async def _manifest_descriptors(
        self,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        content: bytes,
        status: ArtifactStatus,
    ) -> list[ArtifactDescriptor]:
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ArtifactRegistrationError(
                "Artifact manifest must be UTF-8."
            ) from exc
        descriptors: list[ArtifactDescriptor] = []
        for line_number, raw in enumerate(lines, start=1):
            if not raw.strip():
                continue
            try:
                entry = ArtifactManifestEntry.model_validate_json(raw)
                descriptors.append(
                    await manifest_descriptor(driver, workspace, entry, status)
                )
            except Exception as exc:
                raise ArtifactRegistrationError(
                    "Invalid Artifact manifest entry near appended line "
                    f"{line_number}."
                ) from exc
        return descriptors
