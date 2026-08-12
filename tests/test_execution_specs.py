import hashlib
import json
from pathlib import Path

import pytest

from executor_service.domain.enums import CodeSourceType
from executor_service.domain.errors import InvalidExecutionSpecError
from executor_service.execution_specs import ExecutionSpecResolver
from executor_service.interfaces.mcp.schemas import InlineCodeSource, PathCodeSource


def _spec() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "execution_plan_id": "plan-1",
        "steps": [
            {
                "sequence": 0,
                "plan_step_id": "plan-step-1",
                "code": "print('hello')",
            }
        ],
    }


async def test_inline_and_path_resolve_to_the_same_execution_spec(tmp_path: Path) -> None:
    resolver = ExecutionSpecResolver(tmp_path)
    inline = InlineCodeSource.model_validate({"type": "INLINE", "spec": _spec()})
    content = json.dumps(_spec(), indent=2).encode()
    source_file = tmp_path / "plans" / "source.json"
    source_file.parent.mkdir()
    source_file.write_bytes(content)
    path = PathCodeSource(
        type=CodeSourceType.PATH,
        path="plans/source.json",
        sha256=hashlib.sha256(content).hexdigest(),
    )

    inline_result = await resolver.resolve(inline)
    path_result = await resolver.resolve(path)

    assert inline_result.spec == path_result.spec
    assert inline_result.canonical_content == path_result.canonical_content
    assert path_result.sha256 == hashlib.sha256(content).hexdigest()


async def test_path_rejects_hash_mismatch_and_pv_escape(tmp_path: Path) -> None:
    resolver = ExecutionSpecResolver(tmp_path)
    source_file = tmp_path / "source.json"
    source_file.write_text(json.dumps(_spec()), encoding="utf-8")

    with pytest.raises(InvalidExecutionSpecError, match="SHA-256"):
        await resolver.resolve(
            PathCodeSource(
                type=CodeSourceType.PATH,
                path="source.json",
                sha256="0" * 64,
            )
        )
    with pytest.raises(InvalidExecutionSpecError, match="outside"):
        await resolver.resolve(
            PathCodeSource(
                type=CodeSourceType.PATH,
                path="../source.json",
                sha256="0" * 64,
            )
        )


async def test_source_size_limits_are_enforced(tmp_path: Path) -> None:
    inline = InlineCodeSource.model_validate({"type": "INLINE", "spec": _spec()})
    with pytest.raises(InvalidExecutionSpecError, match="INLINE"):
        await ExecutionSpecResolver(tmp_path, inline_max_bytes=1).resolve(inline)

    content = json.dumps(_spec()).encode()
    source_file = tmp_path / "source.json"
    source_file.write_bytes(content)
    path = PathCodeSource(
        type=CodeSourceType.PATH,
        path="source.json",
        sha256=hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(InvalidExecutionSpecError, match="file size"):
        await ExecutionSpecResolver(tmp_path, file_max_bytes=1).resolve(path)
