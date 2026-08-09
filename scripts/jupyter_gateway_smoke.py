"""Exercise Jupyter REST and WebSocket APIs without exposing credentials."""

import asyncio

from executor_service.config import get_settings
from executor_service.infrastructure.jupyter import JupyterGateway


async def main() -> None:
    settings = get_settings()
    relative_path = "users/smoke/projects/smoke/sessions/smoke/executions/gateway-smoke"
    (settings.workspace_host_root / relative_path).mkdir(parents=True, exist_ok=True)
    gateway = JupyterGateway(
        settings.jupyter_endpoint,
        settings.jupyter_auth_token,
        settings.jupyter_request_timeout_seconds,
    )
    kernel_id: str | None = None
    try:
        await gateway.status()
        kernels = await gateway.kernel_specs()
        print("kernels:", kernels)
        kernel_id = await gateway.start_kernel("python3", relative_path)
        result = await gateway.execute_cell(kernel_id, "value = 40 + 2\nprint(value)\nvalue")
        print("execution_count:", result.execution_count)
        print("outputs:", result.outputs)
    finally:
        if kernel_id is not None:
            await gateway.delete_kernel(kernel_id)
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
