"""Measure kernels from /proc without running code in them.

Kernels are child processes of this server inside one container, so a cgroup
cannot tell them apart. A kernel here is its provisioner's process and every
process below it that is still running. Memory is resident set size (RSS),
the measure Linux keeps a peak of; pages shared by forked children count once
per process, so a kernel with children reads high, never low.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

PROC_ROOT = Path("/proc")
_CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
# Writing 5 to clear_refs resets a process's peak RSS (VmHWM) to its RSS now.
_RESET_PEAK = "5"


def kernel_pid(kernel: Any) -> int | None:
    process = getattr(kernel.provisioner, "process", None)
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) else None


def kernel_usages(
    pids: Mapping[str, int | None], proc: Path = PROC_ROOT
) -> dict[str, dict[str, Any]]:
    """Current memory and CPU of each kernel, from one scan of /proc."""

    processes = _processes(proc)
    children = _children(processes)
    uptime = _uptime_seconds(proc)
    usages = {}
    for kernel_id, pid in pids.items():
        tree = _tree(pid, processes, children)
        rss = [_status_bytes(proc, member, "VmRSS:") for member in tree]
        usages[kernel_id] = {
            "process_count": len(tree) or None,
            "memory_bytes": _total(rss),
            "cpu_seconds": _cpu_seconds(processes, tree),
            "age_seconds": _age_seconds(processes, tree, uptime),
        }
    return usages


class StepUsage:
    """Peak memory and CPU time of one kernel from creation to finish()."""

    def __init__(self, pid: int | None, proc: Path = PROC_ROOT) -> None:
        self._pid = pid
        self._proc = proc
        processes = _processes(proc)
        tree = _tree(pid, processes, _children(processes))
        self._cpu_before = _cpu_seconds(processes, tree)
        # Without a reset the peak would cover the kernel's whole life.
        self._peak_reset = bool(tree) and all(
            _reset_peak(proc, member) for member in tree
        )

    def finish(self) -> dict[str, Any]:
        processes = _processes(self._proc)
        tree = _tree(self._pid, processes, _children(processes))
        peaks = [_status_bytes(self._proc, pid, "VmHWM:") for pid in tree]
        cpu_after = _cpu_seconds(processes, tree)
        return {
            # Processes that ended during the Step take their peak with them;
            # children started during it report their whole, in-Step life.
            "peak_memory_bytes": (
                _total(peaks) if self._peak_reset and tree else None
            ),
            # A child moving out of the tree takes its time with it.
            "cpu_seconds": (
                round(max(0.0, cpu_after - self._cpu_before), 3)
                if cpu_after is not None and self._cpu_before is not None
                else None
            ),
        }


def _processes(proc: Path) -> dict[int, tuple[int, int, int]]:
    """Each PID's parent, CPU ticks with its reaped children, and start tick."""

    processes = {}
    try:
        entries = list(proc.iterdir())
    except OSError:
        return processes
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            # The command name may hold spaces and parentheses.
            fields = (entry / "stat").read_text().rpartition(")")[2].split()
            processes[int(entry.name)] = (
                int(fields[1]),
                sum(int(value) for value in fields[11:15]),
                int(fields[19]),
            )
        except (OSError, ValueError, IndexError):
            continue
    return processes


def _children(
    processes: Mapping[int, tuple[int, int, int]],
) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for child, (parent, _ticks, _start) in processes.items():
        children.setdefault(parent, []).append(child)
    return children


def _tree(
    pid: int | None,
    processes: Mapping[int, tuple[int, int, int]],
    children: Mapping[int, list[int]],
) -> list[int]:
    if pid is None or pid not in processes:
        return []
    tree, pending = [], [pid]
    while pending:
        current = pending.pop()
        tree.append(current)
        pending.extend(children.get(current, ()))
    return tree


def _cpu_seconds(
    processes: Mapping[int, tuple[int, int, int]], tree: Iterable[int]
) -> float | None:
    members = list(tree)
    if not members:
        return None
    return round(sum(processes[pid][1] for pid in members) / _CLOCK_TICKS, 3)


def _age_seconds(
    processes: Mapping[int, tuple[int, int, int]],
    tree: list[int],
    uptime: float | None,
) -> float | None:
    """Seconds since the kernel process started, on this host's clock."""

    if not tree or uptime is None:
        return None
    return round(max(0.0, uptime - processes[tree[0]][2] / _CLOCK_TICKS), 3)


def _uptime_seconds(proc: Path) -> float | None:
    try:
        return float((proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _status_bytes(proc: Path, pid: int, key: str) -> int | None:
    try:
        for line in (proc / str(pid) / "status").read_text().splitlines():
            if line.startswith(key):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _reset_peak(proc: Path, pid: int) -> bool:
    try:
        (proc / str(pid) / "clear_refs").write_text(_RESET_PEAK)
    except OSError:
        return False
    return True


def _total(values: list[int | None]) -> int | None:
    # A process that ended between the scan and the read left nothing to count.
    present = [value for value in values if value is not None]
    return sum(present) if present else None
