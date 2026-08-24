import hashlib
from pathlib import Path

import pytest

from executor_service.domain.errors import InvalidExecutionSpecError
from executor_service.execution_specs import (
    ExecutionSpec,
    ExecutionSpecResolver,
)


def _spec(source: dict[str, str]) -> ExecutionSpec:
    return ExecutionSpec.model_validate(
        {
            "schema_version": "1.0",
            "steps": [
                {
                    "sequence": 0,
                    "payload": {"type": "PYTHON_EXECUTE", "source": source},
                }
            ],
        }
    )


async def test_inline_and_path_resolve_to_the_same_step_content(
    tmp_path: Path,
) -> None:
    resolver = ExecutionSpecResolver(tmp_path)
    code = "print('hello')"
    inline = _spec({"type": "INLINE", "content": code})
    source_file = tmp_path / "steps" / "step-0.py"
    source_file.parent.mkdir()
    source_file.write_text(code, encoding="utf-8")
    checksum = hashlib.sha256(code.encode()).hexdigest()
    path = _spec(
        {"type": "PATH", "path": "steps/step-0.py", "sha256": checksum}
    )

    inline_result = await resolver.resolve(inline)
    path_result = await resolver.resolve(path)

    assert inline_result.spec.schema_version == "1.0"
    assert inline_result.steps[0].content == path_result.steps[0].content
    assert path_result.steps[0].source_path == "steps/step-0.py"
    assert path_result.steps[0].source_sha256 == checksum


async def test_path_rejects_hash_mismatch_and_pv_escape(
    tmp_path: Path,
) -> None:
    resolver = ExecutionSpecResolver(tmp_path)
    source_file = tmp_path / "source.py"
    source_file.write_text("print('hello')", encoding="utf-8")

    with pytest.raises(InvalidExecutionSpecError, match="SHA-256"):
        await resolver.resolve(
            _spec({"type": "PATH", "path": "source.py", "sha256": "0" * 64})
        )
    with pytest.raises(InvalidExecutionSpecError, match="outside"):
        await resolver.resolve(
            _spec({"type": "PATH", "path": "../source.py", "sha256": "0" * 64})
        )


async def test_source_size_limits_are_enforced(tmp_path: Path) -> None:
    inline = _spec({"type": "INLINE", "content": "print('hello')"})
    with pytest.raises(InvalidExecutionSpecError, match="INLINE"):
        await ExecutionSpecResolver(tmp_path, inline_max_bytes=1).resolve(
            inline
        )

    code = "print('hello')"
    source_file = tmp_path / "source.py"
    source_file.write_text(code, encoding="utf-8")
    path = _spec(
        {
            "type": "PATH",
            "path": "source.py",
            "sha256": hashlib.sha256(code.encode()).hexdigest(),
        }
    )
    with pytest.raises(InvalidExecutionSpecError, match="file size"):
        await ExecutionSpecResolver(tmp_path, file_max_bytes=1).resolve(path)


def test_only_execution_spec_version_1_is_accepted() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        ExecutionSpec.model_validate(
            {
                "schema_version": "2.0",
                "steps": [
                    {
                        "sequence": 0,
                        "payload": {
                            "type": "PYTHON_EXECUTE",
                            "source": {"type": "INLINE", "content": "pass"},
                        },
                    }
                ],
            }
        )
