"""Jupyter status, profile, and resource observations."""

from datetime import datetime
from typing import Any

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeResourceMetric,
    RuntimeResourceObservation,
)
from executor_service.infrastructure._jupyter.transport import (
    JupyterHttpTransport,
)


class JupyterObservabilityClient:
    def __init__(self, transport: JupyterHttpTransport) -> None:
        self._transport = transport

    async def status(self) -> dict[str, Any]:
        response = await self._transport.request("GET", "/api/status")
        try:
            payload = response.json()
            active_session_count = payload.get("kernels")
            if (
                type(active_session_count) is not int
                or active_session_count < 0
            ):
                raise TypeError("kernels must be a non-negative integer")
            return {"active_session_count": active_session_count}
        except (TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter status response is invalid."
            ) from exc

    async def supported_profiles(self) -> list[str]:
        response = await self._transport.request("GET", "/api/kernelspecs")
        return sorted(response.json().get("kernelspecs", {}).keys())

    async def resource_status(self) -> RuntimeResourceObservation:
        response = await self._transport.request(
            "GET", "/executor/resource-status"
        )
        try:
            payload = response.json()
            if payload.get("schema_version") != "1.0":
                raise ValueError("unsupported resource schema version")
            observed_at = datetime.fromisoformat(
                str(payload["observed_at"]).replace("Z", "+00:00")
            )
            if observed_at.tzinfo is None:
                raise ValueError("observed_at must include a timezone")
            cpu = resource_metric(
                payload["cpu"],
                used_key="used_cores",
                capacity_key="capacity_cores",
            )
            memory = resource_metric(
                payload["memory"],
                used_key="used_bytes",
                capacity_key="capacity_bytes",
            )
            process_count = payload.get("process_count")
            if process_count is not None and not isinstance(
                process_count, int
            ):
                raise TypeError("process_count must be an integer")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter resource response is invalid."
            ) from exc
        return RuntimeResourceObservation(
            observed_at=observed_at,
            process_count=process_count,
            cpu=cpu,
            memory=memory,
        )


def resource_metric(
    payload: object, *, used_key: str, capacity_key: str
) -> RuntimeResourceMetric:
    if not isinstance(payload, dict):
        raise TypeError("resource metric must be an object")
    used = payload.get(used_key)
    capacity = payload.get(capacity_key)
    utilization = payload.get("utilization")
    if used is not None and not isinstance(used, (int, float)):
        raise TypeError(f"{used_key} must be numeric")
    if capacity is not None and not isinstance(capacity, (int, float)):
        raise TypeError(f"{capacity_key} must be numeric")
    if utilization is not None and not isinstance(utilization, (int, float)):
        raise TypeError("utilization must be numeric")
    errors = payload.get("errors", [])
    if not isinstance(errors, list) or not all(
        isinstance(error, str) for error in errors
    ):
        raise TypeError("errors must be a string array")
    return RuntimeResourceMetric(
        used=used,
        capacity=capacity,
        utilization=float(utilization) if utilization is not None else None,
        source=str(payload["source"])
        if payload.get("source") is not None
        else None,
        estimated=payload.get("estimated")
        if isinstance(payload.get("estimated"), bool)
        else None,
        errors=tuple(errors),
    )
