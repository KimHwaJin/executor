"""Observe kernel process continuity without running code in the kernel."""

from __future__ import annotations

from typing import Any
from uuid import uuid4
from weakref import WeakKeyDictionary


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
