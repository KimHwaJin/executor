from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

import psutil  # ty: ignore[unresolved-import]

CGROUP_V2 = "CGROUP_V2"
PSUTIL = "PSUTIL"


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    process_count: int
    memory_rss_bytes: int
    cpu_seconds_by_process: dict[tuple[int, float], float]


class ResourceCollector:
    def __init__(
        self,
        *,
        cgroup_root: Path,
        configured_cpu_cores: float | None,
        configured_memory_bytes: int | None,
        process_iter: Callable[..., Iterable[Any]] = psutil.process_iter,
        uid: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cgroup_root = cgroup_root
        self._configured_cpu_cores = configured_cpu_cores
        self._configured_memory_bytes = configured_memory_bytes
        self._process_iter = process_iter
        self._uid = os.getuid() if uid is None else uid
        self._monotonic = monotonic
        self._lock = Lock()
        self._previous_observed_at: float | None = None
        self._previous_cgroup_cpu_seconds: float | None = None
        self._previous_process_cpu: dict[tuple[int, float], float] = {}

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
            process_snapshot = self._collect_process_snapshot()
            cpu = self._collect_cpu(process_snapshot, elapsed)
            memory = self._collect_memory(process_snapshot)
            self._previous_observed_at = observed_monotonic
            self._previous_process_cpu = process_snapshot.cpu_seconds_by_process

        return {
            "schema_version": "1.0",
            "process_count": process_snapshot.process_count,
            "cpu": cpu,
            "memory": memory,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def _collect_cpu(
        self, process_snapshot: ProcessSnapshot, elapsed: float | None
    ) -> dict[str, Any]:
        errors: list[str] = []
        cumulative_seconds: float | None = None
        source = CGROUP_V2
        try:
            cumulative_seconds = _read_cpu_usage_seconds(self._cgroup_root / "cpu.stat")
        except (OSError, ValueError) as exc:
            source = PSUTIL
            errors.append(_safe_error_code("cgroup_cpu", exc))

        used_cores: float | None
        if source == CGROUP_V2:
            used_cores = _rate(cumulative_seconds, self._previous_cgroup_cpu_seconds, elapsed)
            self._previous_cgroup_cpu_seconds = cumulative_seconds
        else:
            used_cores = _process_cpu_rate(
                process_snapshot.cpu_seconds_by_process,
                self._previous_process_cpu,
                elapsed,
            )
            self._previous_cgroup_cpu_seconds = None

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
            "source": source,
            "estimated": source != CGROUP_V2,
            "errors": errors,
        }

    def _collect_memory(self, process_snapshot: ProcessSnapshot) -> dict[str, Any]:
        errors: list[str] = []
        source = CGROUP_V2
        try:
            used_bytes = _read_positive_int(self._cgroup_root / "memory.current", allow_zero=True)
        except (OSError, ValueError) as exc:
            source = PSUTIL
            used_bytes = process_snapshot.memory_rss_bytes
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
            "source": source,
            "estimated": source != CGROUP_V2,
            "errors": errors,
        }

    def _collect_process_snapshot(self) -> ProcessSnapshot:
        memory_rss_bytes = 0
        cpu_seconds_by_process: dict[tuple[int, float], float] = {}
        process_count = 0
        attributes = ["pid", "uids", "memory_info", "cpu_times", "create_time"]
        for process in self._process_iter(attributes):
            try:
                info = process.info
                if info["uids"].real != self._uid:
                    continue
                memory_rss_bytes += max(0, int(info["memory_info"].rss))
                cpu_times = info["cpu_times"]
                cpu_seconds = max(0.0, float(cpu_times.user + cpu_times.system))
                identity = (int(info["pid"]), float(info["create_time"]))
                cpu_seconds_by_process[identity] = cpu_seconds
                process_count += 1
            except (KeyError, TypeError, ValueError, psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return ProcessSnapshot(
            process_count=process_count,
            memory_rss_bytes=memory_rss_bytes,
            cpu_seconds_by_process=cpu_seconds_by_process,
        )


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


def _rate(current: float | None, previous: float | None, elapsed: float | None) -> float | None:
    if current is None or previous is None or elapsed is None:
        return None
    delta = current - previous
    if delta < 0:
        return None
    return delta / elapsed


def _process_cpu_rate(
    current: dict[tuple[int, float], float],
    previous: dict[tuple[int, float], float],
    elapsed: float | None,
) -> float | None:
    if elapsed is None or not previous:
        return None
    cpu_delta = sum(
        max(0.0, total - previous.get(identity, total)) for identity, total in current.items()
    )
    return cpu_delta / elapsed


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _safe_error_code(prefix: str, exc: Exception) -> str:
    return f"{prefix}:{type(exc).__name__}"
