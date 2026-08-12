"""Exercise Jupyter REST and WebSocket APIs without exposing credentials."""

import asyncio

from executor_service.config import get_settings
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver


async def main() -> None:
    settings = get_settings()
    relative_path = "users/smoke/projects/smoke/sessions/smoke/executions/gateway-smoke"
    (settings.workspace_host_root / relative_path).mkdir(parents=True, exist_ok=True)
    gateway = JupyterRuntimeDriver(
        settings.jupyter_endpoint,
        settings.jupyter_auth_token,
        settings.jupyter_request_timeout_seconds,
    )
    runtime_session_id: str | None = None
    try:
        await gateway.status()
        kernels = await gateway.supported_profiles()
        print("kernels:", kernels)
        runtime_session_id = await gateway.start_session("python3", relative_path)
        result = await gateway.execute(runtime_session_id, "value = 40 + 2\nprint(value)\nvalue")
        print("execution_count:", result.execution_count)
        print("outputs:", result.outputs)
    finally:
        if runtime_session_id is not None:
            await gateway.delete_session(runtime_session_id)
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
