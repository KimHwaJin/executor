"""Observe kernel process continuity without running code in the kernel."""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any
from uuid import uuid4
from weakref import WeakKeyDictionary

CGROUP_ROOT = Path("/sys/fs/cgroup")


class KernelStateObserver:
    def __init__(self) -> None:
        # A server restart invalidates all previously observed identities.
        self._server_id = uuid4().hex
        self._instances: WeakKeyDictionary[Any, str] = WeakKeyDictionary()

    async def observe(self, manager: Any, kernel_id: str) -> dict[str, Any]:
        missing = {
            "kernel_id": kernel_id,
            "alive": False,
            "instance_id": None,
        }
        # MappingKernelManager raises HTTPError(404), unlike the client's
        # KeyError. Membership is a non-blocking public check on both managers.
        if kernel_id not in manager:
            return missing
        try:
            kernel = manager.get_kernel(kernel_id)
        except KeyError:
            return missing
        if kernel.shutting_down:
            return missing
        process = getattr(kernel.provisioner, "process", None)
        alive = await kernel.is_alive()
        # The LocalProvisioner replaces its process on both automatic and
        # manual restarts, even when the Kernel ID/connection file is reused.
        if not alive or kernel.shutting_down:
            return missing
        if process is None:
            raise RuntimeError("Kernel process continuity is unavailable.")
        if kernel_id not in manager:
            return missing
        try:
            current = manager.get_kernel(kernel_id)
        except KeyError:
            return missing
        if current is not kernel or kernel.provisioner.process is not process:
            return missing
        instance = self._instances.get(process)
        if instance is None:
            instance = f"{self._server_id}:{uuid4().hex}"
            self._instances[process] = instance
        return {
            "kernel_id": kernel_id,
            "alive": True,
            "instance_id": instance,
        }


def memory_limit_events(root: Path = CGROUP_ROOT) -> int | None:
    """Times this container's memory cgroup hit its limit, if readable.

    cgroup v2 counts OOM kills; cgroup v1 on older kernels only counts
    allocations that hit the limit (memory.failcnt).
    """

    try:
        for line in (root / "memory.events").read_text().splitlines():
            name, _, value = line.partition(" ")
            if name == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        pass
    try:
        return int((root / "memory" / "memory.failcnt").read_text())
    except (OSError, ValueError):
        return None


def exit_reason(
    process: Any, memory_events_before: int | None, memory_events: int | None
) -> str:
    """Explain how a stopped kernel process ended, as far as is known."""

    returncode = None if process is None else process.poll()
    if returncode is None:
        return ""
    if returncode >= 0:
        return f" It exited with code {returncode}."
    try:
        name = signal.Signals(-returncode).name
    except ValueError:
        name = f"signal {-returncode}"
    reason = f" It was killed by {name}."
    if (
        returncode == -signal.SIGKILL
        and memory_events_before is not None
        and memory_events is not None
        and memory_events > memory_events_before
    ):
        # The OOM killer always uses SIGKILL. Without per-process OOM
        # records this is an inference, not proof.
        reason += (
            " The container reached its memory limit during execution, so "
            "it was most likely killed for running out of memory."
        )
    return reason
