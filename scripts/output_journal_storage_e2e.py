"""Verify JSONL text output and native image Output Journal storage."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from execution_spec_payload import execution_request, inline_spec
from mcp import Client

TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


async def _required(
    client: Client, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        raise RuntimeError(f"{tool} failed: {result.content}")
    return result.structured_content


async def _wait_terminal(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(300):
        execution = await _required(
            client, "execution_get", {"execution_id": execution_id}
        )
        if execution["state"]["status"] in TERMINAL_STATES:
            return execution
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish.")


def _assert_runtime_storage(
    workspace_path: str, notebook_path: str
) -> tuple[Path, Path, Path]:
    runtime_root = Path(
        os.getenv(
            "JUPYTER_WORKSPACE_HOST_ROOT",
            "test_harness/jupyter/workspace",
        )
    ).resolve()
    workspace = runtime_root / workspace_path
    output_root = workspace / "outputs"
    journals = list(output_root.rglob("journal.jsonl"))
    images = list(output_root.rglob("*.png"))
    if len(journals) != 1 or len(images) != 1:
        raise RuntimeError(
            f"Unexpected journal layout: journals={journals}, images={images}"
        )
    if list(output_root.rglob("*.bin")):
        raise RuntimeError("Legacy representation .bin files were created.")
    if list(output_root.rglob("source.py")):
        raise RuntimeError("Legacy source.py files were created.")
    if list(output_root.rglob("batches")):
        raise RuntimeError("Legacy batch directories were created.")
    entries = [
        json.loads(line)
        for line in journals[0].read_text(encoding="utf-8").splitlines()
    ]
    record_types = [entry["record_type"] for entry in entries]
    if (
        record_types[0] != "HEADER"
        or record_types[-1] != "TERMINAL"
        or not record_types[1:-1]
        or any(value != "BATCH" for value in record_types[1:-1])
    ):
        raise RuntimeError(f"Unexpected Journal records: {record_types}")
    image_body = images[0].read_bytes()
    if not image_body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Journal image is not a native PNG file.")
    width = int.from_bytes(image_body[16:20], "big")
    height = int.from_bytes(image_body[20:24], "big")
    if width < 800 or height < 400 or len(image_body) < 10_000:
        raise RuntimeError(
            f"Journal chart is unexpectedly small: {width}x{height}, "
            f"{len(image_body)} bytes"
        )

    notebook = json.loads((runtime_root / notebook_path).read_text("utf-8"))
    outputs = notebook["cells"][0]["outputs"]
    image_values = [
        output.get("data", {}).get("image/png")
        for output in outputs
        if isinstance(output, dict)
    ]
    if not any(
        isinstance(value, str) and base64.b64decode(value) == image_body
        for value in image_values
    ):
        raise RuntimeError(
            "Completed notebook does not contain the native Journal PNG."
        )
    artifact = workspace / "artifacts" / "plots" / "quality-dashboard.png"
    artifact_body = artifact.read_bytes()
    if (
        not artifact_body.startswith(b"\x89PNG\r\n\x1a\n")
        or len(artifact_body) < 10_000
    ):
        raise RuntimeError("Plot artifact is not a complete PNG chart.")
    return journals[0], images[0], artifact


async def main() -> None:
    unique = uuid4().hex
    user_id = f"journal-e2e-user-{unique}"
    request = execution_request(
        idempotency_key=f"journal-e2e-submit-{unique}",
        operation_mode="SINGLE",
        trigger_type="INTERACTIVE",
        runtime_profile="basic",
        spec=inline_spec(
            [
                {
                    "skill_name": "eda",
                    "tool_name": "production_quality_dashboard",
                    "code": (
                        "from pathlib import Path\n"
                        "import matplotlib.pyplot as plt\n"
                        "import numpy as np\n"
                        "import pandas as pd\n"
                        "import seaborn as sns\n"
                        "from IPython.display import display\n"
                        "from matplotlib_inline.backend_inline import "
                        "set_matplotlib_formats\n"
                        "set_matplotlib_formats('png')\n"
                        "sns.set_theme(style='whitegrid')\n"
                        "weeks = pd.date_range('2026-05-04', periods=12, "
                        "freq='W-MON')\n"
                        "quality = pd.DataFrame({\n"
                        "    'week': weeks,\n"
                        "    'yield_rate': [92.4, 93.1, 92.8, 94.0, 94.6, "
                        "95.2, 95.0, 96.1, 96.5, 96.2, 97.0, 97.4],\n"
                        "    'defects': [184, 171, 176, 151, 139, 126, 130, "
                        "108, 99, 103, 82, 74],\n"
                        "})\n"
                        "labels = quality['week'].dt.strftime('%m-%d')\n"
                        "x = np.arange(len(quality))\n"
                        "fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), "
                        "sharex=True, gridspec_kw={'height_ratios': [2, 1]})\n"
                        "ax1.plot(x, quality['yield_rate'], marker='o', "
                        "linewidth=2.8, color='#2563eb')\n"
                        "ax1.axhline(95, color='#ef4444', linestyle='--', "
                        "linewidth=1.4, label='Target 95%')\n"
                        "ax1.fill_between(x, 95, "
                        "quality['yield_rate'], where=quality['yield_rate'] "
                        ">= 95, color='#22c55e', alpha=0.15)\n"
                        "ax1.set(title='Line A Weekly Production Quality', "
                        "ylabel='First-pass yield (%)', ylim=(91.5, 98.2))\n"
                        "ax1.legend(loc='lower right')\n"
                        "ax2.bar(x, quality['defects'], color='#f59e0b', "
                        "width=0.68)\n"
                        "ax2.set(ylabel='Defect count', xlabel='Week')\n"
                        "ax2.set_xticks(range(len(labels)), labels, rotation=35)\n"
                        "fig.suptitle('Manufacturing Quality Improvement', "
                        "fontsize=16, fontweight='bold')\n"
                        "fig.tight_layout()\n"
                        "plot_dir = Path('artifacts/plots')\n"
                        "plot_dir.mkdir(parents=True, exist_ok=True)\n"
                        "fig.savefig(plot_dir / 'quality-dashboard.png', "
                        "dpi=160, bbox_inches='tight')\n"
                        "display(fig)\n"
                        "plt.close(fig)\n"
                        "print('Yield improved from 92.4% to 97.4%; defects "
                        "fell from 184 to 74.')"
                    ),
                }
            ]
        ),
        context={
            "user_id": user_id,
            "project_id": "journal-e2e-project",
            "session_id": f"journal-e2e-session-{unique}",
            "task_id": f"journal-e2e-task-{unique}",
        },
        actor={"type": "USER", "id": user_id},
    )
    async with Client(
        os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp")
    ) as client:
        submitted = await _required(
            client, "execution_submit", {"request": request}
        )
        execution_id = str(submitted["execution_id"])
        terminal = await _wait_terminal(client, execution_id)
        outputs = await _required(
            client,
            "execution_output_list",
            {"execution_id": execution_id, "limit": 200},
        )
        representations = [
            (output["output_id"], output["kind"], representation)
            for output in outputs["items"]
            for representation in output["representations"]
        ]
        text_output_id, _, text_representation = next(
            value
            for value in representations
            if value[1] == "STREAM"
            and value[2]["media_type"] == "text/plain"
            and value[2]["size_bytes"] > 0
        )
        image_output_id, _, image_representation = next(
            value
            for value in representations
            if value[2]["media_type"] == "image/png"
        )
        text_content = await _required(
            client,
            "execution_output_content_get",
            {
                "execution_id": execution_id,
                "output_id": text_output_id,
                "representation_id": text_representation["representation_id"],
            },
        )
        image_content = await _required(
            client,
            "execution_output_content_get",
            {
                "execution_id": execution_id,
                "output_id": image_output_id,
                "representation_id": image_representation["representation_id"],
            },
        )

    if text_content["delivery"] != "INLINE" or "Yield improved" not in str(
        text_content["content"]
    ):
        raise RuntimeError(f"MCP text delivery is invalid: {text_content}")
    if image_content["delivery"] != "HTTP" or image_content["content"]:
        raise RuntimeError(f"MCP image delivery is invalid: {image_content}")
    async with httpx.AsyncClient(
        base_url=os.getenv("EXECUTOR_HTTP_URL", "http://127.0.0.1:8000")
    ) as http:
        image_response = await http.get(image_content["content_url"])
        range_response = await http.get(
            image_content["content_url"], headers={"Range": "bytes=0-63"}
        )
    image_response.raise_for_status()
    range_response.raise_for_status()
    if image_response.headers.get("content-type") != "image/png":
        raise RuntimeError("REST content did not preserve image/png.")
    if (
        hashlib.sha256(image_response.content).hexdigest()
        != image_representation["checksum_sha256"]
    ):
        raise RuntimeError("REST image checksum does not match metadata.")
    if (
        range_response.status_code != 206
        or range_response.content != image_response.content[:64]
        or range_response.headers.get("content-range")
        != f"bytes 0-63/{len(image_response.content)}"
    ):
        raise RuntimeError("REST image Range response is invalid.")

    if terminal["state"]["status"] != "SUCCEEDED":
        raise RuntimeError(f"Journal E2E Execution failed: {terminal}")
    workspace = terminal["workspace"]
    journal, image, artifact = await asyncio.to_thread(
        _assert_runtime_storage,
        str(workspace["path"]),
        str(workspace["notebook_path"]),
    )
    if image.read_bytes() != image_response.content:
        raise RuntimeError(
            "Public REST image differs from the native Runtime file."
        )
    print("execution_id:", execution_id)
    print("journal:", journal)
    print("image:", image)
    print("artifact:", artifact)
    print("notebook:", workspace["notebook_path"])
    print("mcp_text_delivery:", text_content["delivery"])
    print("mcp_image_delivery:", image_content["delivery"])
    print("rest_image_bytes:", len(image_response.content))
    print("rest_range:", range_response.headers["content-range"])


if __name__ == "__main__":
    asyncio.run(main())
