"""Verify automatic and Manifest Artifact registration through a real Jupyter execution."""

import asyncio
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request, inline_spec
from mcp import Client


async def _wait(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(200):
        result = await client.call_tool(
            "execution_get", {"execution_id": execution_id}
        )
        state = result.structured_content
        if state["state"]["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish.")


async def main() -> None:
    unique = str(uuid4())
    user_id = "artifact-smoke-user"
    processed_relative = (
        f"users/{user_id}/datasets/processed/{unique}/processed.csv"
    )
    write_files_code = (
        "from pathlib import Path\n"
        "Path('artifacts/plots/plot.png').write_bytes(b'fake-png')\n"
        "Path('artifacts/reports/summary.md').write_text('# Summary', encoding='utf-8')\n"
    )
    publish_manifest_code = (
        "import json\n"
        f"processed = Path('/workspace/pv/{processed_relative}')\n"
        "processed.parent.mkdir(parents=True, exist_ok=True)\n"
        "processed.write_text('value\\n1\\n', encoding='utf-8')\n"
        "entries = [\n"
        "    {\n"
        "        'storage_type': 'PV',\n"
        "        'artifact_type': 'DATASET',\n"
        "        'path': str(processed),\n"
        "        'name': 'processed-data',\n"
        "        'external_parent_asset_id': 'raw-daily-data',\n"
        "        'metadata': {'rows': 1, 'token': 'must-not-leak'},\n"
        "    },\n"
        "    {\n"
        "        'storage_type': 'S3',\n"
        "        'artifact_type': 'MODEL',\n"
        f"        'uri': 's3://analysis-results/models/{unique}.onnx',\n"
        "        'name': 'trained-model',\n"
        "        'size_bytes': 42,\n"
        f"        'checksum_sha256': '{'b' * 64}',\n"
        "    },\n"
        "]\n"
        "manifest = Path('artifacts/manifest.jsonl')\n"
        "with manifest.open('a', encoding='utf-8') as handle:\n"
        "    for entry in entries:\n"
        "        handle.write(json.dumps(entry) + '\\n')\n"
    )

    async with Client("http://127.0.0.1:8000/mcp") as client:
        submitted = await client.call_tool(
            "execution_submit",
            {
                "request": execution_request(
                    idempotency_key=f"artifact-smoke-{unique}",
                    operation_mode="SINGLE",
                    trigger_type="INTERACTIVE",
                    actor={"type": "USER", "id": user_id},
                    runtime_profile="basic",
                    spec=inline_spec(
                        [
                            {
                                "skill_name": "report",
                                "tool_name": "write_files",
                                "code": write_files_code,
                            },
                            {
                                "skill_name": "data_preprocess",
                                "tool_name": "publish_manifest",
                                "code": publish_manifest_code,
                            },
                        ],
                    ),
                    context={
                        "user_id": user_id,
                        "project_id": "artifact-smoke-project",
                        "session_id": "artifact-smoke-session",
                        "task_id": f"artifact-smoke-task-{unique}",
                    },
                )
            },
        )
        execution_id = submitted.structured_content["execution_id"]
        terminal = await _wait(client, execution_id)
        if terminal["state"]["status"] != "SUCCEEDED":
            raise RuntimeError(f"Artifact execution failed: {terminal}")

        listed = await client.call_tool(
            "execution_artifact_list", {"execution_id": execution_id}
        )
        artifacts = listed.structured_content["items"]
        if len(artifacts) != 5:
            raise RuntimeError(f"Expected five Artifacts: {artifacts}")
        artifact_types = {item["type"] for item in artifacts}
        if artifact_types != {
            "PLOT",
            "REPORT",
            "DATASET",
            "MODEL",
            "NOTEBOOK",
        }:
            raise RuntimeError(f"Unexpected Artifact types: {artifact_types}")
        processed_artifact = next(
            item for item in artifacts if item["name"] == "processed-data"
        )
        fetched = await client.call_tool(
            "execution_artifact_get",
            {"artifact_id": processed_artifact["artifact_id"]},
        )
        processed_detail = fetched.structured_content
        if (
            processed_detail["artifact_id"]
            != processed_artifact["artifact_id"]
        ):
            raise RuntimeError(
                "Artifact detail lookup returned the wrong row."
            )
        if (
            processed_detail["lineage"]["external_parent_asset_id"]
            != "raw-daily-data"
            or processed_detail["metadata"]["token"] != "[REDACTED]"
        ):
            raise RuntimeError(
                f"Lineage or redaction failed: {processed_detail}"
            )
    if processed_detail["storage"]["size_bytes"] <= 0:
        raise RuntimeError(
            "Expected Runtime-computed processed data metadata."
        )
    print("execution_id:", execution_id)
    print("artifact_count:", len(artifacts))
    print("artifact_types:", sorted(artifact_types))
    print(
        "lineage_parent:",
        processed_detail["lineage"]["external_parent_asset_id"],
    )


if __name__ == "__main__":
    asyncio.run(main())
