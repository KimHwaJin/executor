from typing import Any

import httpx
import pytest

from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver


def _resource_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "process_count": 5,
        "cpu": {
            "used_cores": 0.25,
            "capacity_cores": 2.0,
            "utilization": 0.125,
            "source": "CGROUP_V2",
            "estimated": False,
            "errors": [],
        },
        "memory": {
            "used_bytes": 256,
            "capacity_bytes": 1024,
            "utilization": 0.25,
            "source": "CGROUP_V2",
            "estimated": False,
            "errors": [],
        },
        "observed_at": "2026-08-12T10:00:00Z",
    }


async def test_resource_status_parses_versioned_jupyter_response() -> None:
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=_resource_payload())
        ),
        base_url="http://jupyter.invalid",
    )
    try:
        observation = await driver.resource_status()
    finally:
        await driver.close()

    assert observation.process_count == 5
    assert observation.cpu.utilization == 0.125
    assert observation.memory.used == 256
    assert observation.observed_at.tzinfo is not None


async def test_resource_status_rejects_unknown_schema_version() -> None:
    payload = _resource_payload()
    payload["schema_version"] = "2.0"
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        base_url="http://jupyter.invalid",
    )
    try:
        with pytest.raises(RuntimeDriverError, match="response is invalid"):
            await driver.resource_status()
    finally:
        await driver.close()
