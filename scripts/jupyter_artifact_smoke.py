"""Verify automatic and Manifest Artifact registration through a real Jupyter execution."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp import Client

from executor_service.config import get_settings


async def _wait(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(200):
        result = await client.call_tool("execution_get", {"execution_id": execution_id})
        state = result.structured_content
        if state["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish.")


async def main() -> None:
    unique = str(uuid4())
    user_id = "artifact-smoke-user"
    processed_relative = (
        f"users/{user_id}/datasets/processed/{unique}/processed.csv"
    )
    source = (
        "# %%\n"
        "from pathlib import Path\n"
        "Path('artifacts/plot.png').write_bytes(b'fake-png')\n"
        "Path('reports/summary.md').write_text('# Summary', encoding='utf-8')\n"
        "# %%\n"
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
                "request": {
                    "idempotency_key": f"artifact-smoke-{unique}",
                    "mode": "STATIC",
                    "trigger_type": "INTERACTIVE",
                    "jupyter_pool": "INTERACTIVE",
                    "kernel_name": "python3",
                    "source": {"type": "INLINE", "code": source},
                    "context": {
                        "requested_by_user_id": user_id,
                        "project_id": "artifact-smoke-project",
                        "session_id": "artifact-smoke-session",
                        "execution_plan_id": f"artifact-smoke-plan-{unique}",
                    },
                    "steps": [
                        {"sequence": 0, "skill_name": "report", "tool_name": "write_files"},
                        {
                            "sequence": 1,
                            "skill_name": "data_preprocess",
                            "tool_name": "publish_manifest",
                        },
                    ],
                }
            },
        )
        execution_id = submitted.structured_content["execution_id"]
        terminal = await _wait(client, execution_id)
        if terminal["status"] != "SUCCEEDED":
            raise RuntimeError(f"Artifact execution failed: {terminal}")

        listed = await client.call_tool(
            "execution_artifact_list", {"execution_id": execution_id}
        )
        artifacts = listed.structured_content["result"]
        if len(artifacts) != 5:
            raise RuntimeError(f"Expected five Artifacts: {artifacts}")
        artifact_types = {item["artifact_type"] for item in artifacts}
        if artifact_types != {"PLOT", "REPORT", "DATASET", "MODEL", "NOTEBOOK"}:
            raise RuntimeError(f"Unexpected Artifact types: {artifact_types}")
        processed_artifact = next(
            item for item in artifacts if item["name"] == "processed-data"
        )
        if (
            processed_artifact["external_parent_asset_id"] != "raw-daily-data"
            or processed_artifact["metadata"]["token"] != "[REDACTED]"
        ):
            raise RuntimeError(f"Lineage or redaction failed: {processed_artifact}")

        fetched = await client.call_tool(
            "execution_artifact_get",
            {"artifact_id": processed_artifact["artifact_id"]},
        )
        if fetched.structured_content["artifact_id"] != processed_artifact["artifact_id"]:
            raise RuntimeError("Artifact detail lookup returned the wrong row.")
        trace = await client.call_tool(
            "execution_trace_get", {"execution_id": execution_id}
        )
        if len(trace.structured_content["artifacts"]) != 5:
            raise RuntimeError("Execution Trace did not include every Artifact.")

    host_file = get_settings().workspace_host_root / Path(processed_relative)
    if not host_file.is_file():
        raise RuntimeError("Expected processed PV data was not created.")
    print("execution_id:", execution_id)
    print("artifact_count:", len(artifacts))
    print("artifact_types:", sorted(artifact_types))
    print("lineage_parent:", processed_artifact["external_parent_asset_id"])
    print("trace_artifacts:", len(trace.structured_content["artifacts"]))


if __name__ == "__main__":
    asyncio.run(main())
