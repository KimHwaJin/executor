"""Tests for MCP state plus safe shared-volume result reconciliation."""

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from executor_test_agent.integrations import executor as executor_module


async def test_collect_execution_result_resolves_shared_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    result_dir = tmp_path / "executions/e/operations/o/steps/s/attempts/a/1"
    output_dir = result_dir / "outputs"
    output_dir.mkdir(parents=True)
    content = b"55\n"
    content_path = output_dir / "000000-stream-00.txt"
    content_path.write_bytes(content)
    manifest = {
        "schema_version": "1.0",
        "state": "FINALIZED",
        "identity": {
            "execution_id": "execution-1",
            "operation_id": "operation-1",
            "step_id": "step-1",
            "sequence": 0,
            "execution_attempt_id": "attempt-1",
            "fencing_token": 1,
        },
        "source": {
            "relative_path": "executions/e/sources/s/source.py",
            "checksum_sha256": "a" * 64,
            "size_bytes": 10,
        },
        "outputs": [
            {
                "ordinal": 0,
                "kind": "STREAM",
                "stream_name": "stdout",
                "execution_count": None,
                "representations": [
                    {
                        "media_type": "text/plain",
                        "encoding": "UTF8",
                        "relative_path": "outputs/000000-stream-00.txt",
                        "size_bytes": len(content),
                        "checksum_sha256": hashlib.sha256(content).hexdigest(),
                        "complete": True,
                        "metadata": {},
                    }
                ],
                "metadata": {},
            }
        ],
        "output_count": 1,
        "representation_count": 1,
        "total_size_bytes": len(content),
        "output_summary": {},
        "execution_count": 1,
        "error_message": None,
    }
    manifest_path = result_dir / "manifest.json"
    manifest_body = json.dumps(manifest, separators=(",", ":")).encode()
    manifest_path.write_bytes(manifest_body)

    async def fake_required_result(_client, tool, arguments):
        assert tool == "execution_result_get"
        assert arguments == {"execution_id": "execution-1"}
        return {
            "execution": {"execution_id": "execution-1"},
            "operations": [
                {
                    "steps": [
                        {
                            "result": {
                                "result_ref": {
                                    "storage": "SHARED_PV",
                                    "execution_id": "execution-1",
                                    "step_id": "step-1",
                                    "attempt_id": "attempt-1",
                                    "fencing_token": 1,
                                    "relative_path": manifest_path.relative_to(tmp_path).as_posix(),
                                    "checksum_sha256": hashlib.sha256(manifest_body).hexdigest(),
                                }
                            }
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(executor_module, "required_tool_result", fake_required_result)
    result = await executor_module.collect_execution_result(
        cast(Any, object()), "execution-1", tmp_path
    )

    resolved = result["operations"][0]["steps"][0]["result"]["resolved_result"]
    assert resolved["outputs"][0]["representations"][0]["content"] == "55\n"


def test_shared_result_reader_rejects_path_escape(tmp_path: Path) -> None:
    reference = {
        "storage": "SHARED_PV",
        "execution_id": "execution-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "fencing_token": 1,
        "relative_path": "../secret",
        "checksum_sha256": "a" * 64,
    }
    try:
        executor_module.read_step_result(tmp_path, reference)
    except executor_module.ExecutionResultReadError:
        pass
    else:
        raise AssertionError("unsafe path was accepted")
