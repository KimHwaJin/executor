from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

CGROUP_V2 = "CGROUP_V2"


class ResourceCollector:
    def __init__(
        self,
        *,
        cgroup_root: Path,
        configured_cpu_cores: float | None,
        configured_memory_bytes: int | None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cgroup_root = cgroup_root
        self._configured_cpu_cores = configured_cpu_cores
        self._configured_memory_bytes = configured_memory_bytes
        self._monotonic = monotonic
        self._lock = Lock()
        self._previous_observed_at: float | None = None
        self._previous_cpu_seconds: float | None = None

    @classmethod
    def from_environment(cls) -> ResourceCollector:
        return cls(
            cgroup_root=Path(os.getenv("EXECUTOR_RESOURCE_CGROUP_ROOT", "/sys/fs/cgroup")),
            configured_cpu_cores=_optional_positive_float(os.getenv("EXECUTOR_RESOURCE_CPU_CORES")),
            configured_memory_bytes=_optional_positive_int(
                os.getenv("EXECUTOR_RESOURCE_MEMORY_BYTES")
            ),
        )

    def collect(self) -> dict[str, Any]:
        with self._lock:
            observed_monotonic = self._monotonic()
            elapsed = _positive_delta(observed_monotonic, self._previous_observed_at)
            cpu = self._collect_cpu(elapsed)
            memory = self._collect_memory()
            process_count = self._collect_process_count()
            self._previous_observed_at = observed_monotonic

        return {
            "schema_version": "1.0",
            "process_count": process_count,
            "cpu": cpu,
            "memory": memory,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def _collect_cpu(self, elapsed: float | None) -> dict[str, Any]:
        errors: list[str] = []
        used_cores: float | None = None
        try:
            cumulative_seconds = _read_cpu_usage_seconds(self._cgroup_root / "cpu.stat")
            used_cores = _rate(cumulative_seconds, self._previous_cpu_seconds, elapsed)
            self._previous_cpu_seconds = cumulative_seconds
        except (OSError, ValueError) as exc:
            self._previous_cpu_seconds = None
            errors.append(_safe_error_code("cgroup_cpu", exc))

        capacity_cores = self._configured_cpu_cores
        try:
            cgroup_capacity = _read_cpu_capacity(self._cgroup_root / "cpu.max")
            if cgroup_capacity is not None:
                capacity_cores = cgroup_capacity
        except (OSError, ValueError) as exc:
            errors.append(_safe_error_code("cgroup_cpu_capacity", exc))

        return {
            "used_cores": _rounded(used_cores),
            "capacity_cores": _rounded(capacity_cores),
            "utilization": _ratio(used_cores, capacity_cores),
            "source": CGROUP_V2,
            "estimated": False,
            "errors": errors,
        }

    def _collect_process_count(self) -> int | None:
        try:
            return _read_process_count(self._cgroup_root / "cgroup.procs")
        except (OSError, ValueError):
            return None

    def _collect_memory(self) -> dict[str, Any]:
        errors: list[str] = []
        used_bytes: int | None = None
        try:
            used_bytes = _read_positive_int(self._cgroup_root / "memory.current", allow_zero=True)
        except (OSError, ValueError) as exc:
            errors.append(_safe_error_code("cgroup_memory", exc))

        capacity_bytes = self._configured_memory_bytes
        try:
            cgroup_capacity = _read_memory_capacity(self._cgroup_root / "memory.max")
            if cgroup_capacity is not None:
                capacity_bytes = cgroup_capacity
        except (OSError, ValueError) as exc:
            errors.append(_safe_error_code("cgroup_memory_capacity", exc))

        return {
            "used_bytes": used_bytes,
            "capacity_bytes": capacity_bytes,
            "utilization": _ratio(used_bytes, capacity_bytes),
            "source": CGROUP_V2,
            "estimated": False,
            "errors": errors,
        }


def _read_cpu_usage_seconds(path: Path) -> float:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            values[parts[0]] = parts[1]
    if "usage_usec" not in values:
        raise ValueError("cpu.stat does not contain usage_usec")
    usage_usec = int(values["usage_usec"])
    if usage_usec < 0:
        raise ValueError("usage_usec must not be negative")
    return usage_usec / 1_000_000


def _read_cpu_capacity(path: Path) -> float | None:
    quota, period = path.read_text(encoding="utf-8").strip().split()
    if quota == "max":
        return None
    quota_value = int(quota)
    period_value = int(period)
    if quota_value <= 0 or period_value <= 0:
        raise ValueError("cpu.max values must be positive")
    return quota_value / period_value


def _read_memory_capacity(path: Path) -> int | None:
    raw = path.read_text(encoding="utf-8").strip()
    if raw == "max":
        return None
    value = int(raw)
    if value <= 0:
        raise ValueError("memory.max must be positive")
    return value


def _read_positive_int(path: Path, *, allow_zero: bool = False) -> int:
    value = int(path.read_text(encoding="utf-8").strip())
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError("value must be positive")
    return value


def _read_process_count(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _optional_positive_float(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("configured CPU cores must be a positive finite number")
    return value


def _optional_positive_int(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    value = int(raw)
    if value <= 0:
        raise ValueError("configured memory bytes must be positive")
    return value


def _positive_delta(current: float, previous: float | None) -> float | None:
    if previous is None:
        return None
    delta = current - previous
    return delta if delta > 0 else None


def _rate(current: float, previous: float | None, elapsed: float | None) -> float | None:
    if previous is None or elapsed is None:
        return None
    delta = current - previous
    if delta < 0:
        return None
    return delta / elapsed


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _safe_error_code(prefix: str, exc: Exception) -> str:
    return f"{prefix}:{type(exc).__name__}"
