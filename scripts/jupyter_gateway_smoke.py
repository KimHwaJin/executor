"""Exercise Jupyter REST and journal APIs without exposing credentials."""

import asyncio
import json
import os
from uuid import uuid4

from executor_service.domain.runtime import RuntimeOutputRecord
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver
from executor_service.settings import get_settings


async def _stream_text(
    gateway: JupyterRuntimeDriver, session_id: str, code: str
) -> list[str]:
    texts: list[str] = []

    async def collect(record: RuntimeOutputRecord) -> None:
        if record.kind == "STREAM":
            texts.append(record.representations[0].content)

    request_id = uuid4()
    await gateway.start_execution(session_id, request_id, code)
    await gateway.follow_execution(session_id, request_id, collect)
    await gateway.release_execution(session_id, request_id)
    return texts


async def main() -> None:
    settings = get_settings()
    endpoint = os.getenv("JUPYTER_GATEWAY_ENDPOINT")
    token = os.getenv("JUPYTER_GATEWAY_TOKEN")
    if not endpoint or not token:
        raise RuntimeError(
            "JUPYTER_GATEWAY_ENDPOINT and JUPYTER_GATEWAY_TOKEN are required."
        )
    relative_path = (
        "users/smoke/projects/smoke/sessions/smoke/executions/gateway-smoke"
    )
    gateway = JupyterRuntimeDriver(
        endpoint,
        token,
        settings.jupyter_request_timeout_seconds,
    )
    runtime_session_ids: list[str] = []
    try:
        await gateway.prepare_workspace(relative_path)
        await gateway.status()
        kernels = await gateway.supported_profiles()
        print("kernels:", kernels)
        if kernels != ["3102311", "default"]:
            raise RuntimeError(f"Unexpected kernel profiles: {kernels}")

        probes = {
            "default": ((3, 11), ["pandas", "pyarrow"]),
            "3102311": ((3, 10, 11), []),
        }
        for profile, (version, imports) in probes.items():
            runtime_session_id = await gateway.start_session(
                profile, relative_path, None, {}
            )
            runtime_session_ids.append(runtime_session_id)
            code = (
                "import importlib, json, sys\n"
                f"modules = {imports!r}\n"
                "[importlib.import_module(module) for module in modules]\n"
                "print(json.dumps({'version': list(sys.version_info[:3]), "
                "'imports': modules}))"
            )
            stream_outputs = await _stream_text(
                gateway, runtime_session_id, code
            )
            if not stream_outputs:
                raise RuntimeError(
                    f"{profile} did not return a stream output."
                )
            payload = json.loads("".join(stream_outputs))
            if payload["version"][: len(version)] != list(version):
                raise RuntimeError(
                    f"{profile} uses unexpected Python: {payload['version']}"
                )
            print(profile, payload)
    finally:
        for runtime_session_id in runtime_session_ids:
            await gateway.delete_session(runtime_session_id)
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
