"""Exercise Jupyter REST and WebSocket APIs without exposing credentials."""

import asyncio
import json
import os

from executor_service.infrastructure.jupyter import JupyterRuntimeDriver
from executor_service.settings import get_settings


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
                profile, relative_path
            )
            runtime_session_ids.append(runtime_session_id)
            code = (
                "import importlib, json, sys\n"
                f"modules = {imports!r}\n"
                "[importlib.import_module(module) for module in modules]\n"
                "print(json.dumps({'version': list(sys.version_info[:3]), "
                "'imports': modules}))"
            )
            result = await gateway.execute(runtime_session_id, code)
            stream_outputs = [
                output["text"]
                for output in result.outputs
                if output.get("output_type") == "stream"
            ]
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
