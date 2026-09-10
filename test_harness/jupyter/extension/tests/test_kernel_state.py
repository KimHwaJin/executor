import gc
from types import SimpleNamespace

import pytest
from executor_resource_extension.kernel_state import KernelStateObserver


class Process:
    pass


class Kernel:
    def __init__(self) -> None:
        self.provisioner = SimpleNamespace(process=Process())
        self.alive = True
        self.shutting_down = False

    async def is_alive(self) -> bool:
        return self.alive


class Manager:
    def __init__(self, kernel: Kernel) -> None:
        self.kernels = {"kernel": kernel}

    def get_kernel(self, key: str) -> Kernel:
        return self.kernels[key]

    def __contains__(self, key: str) -> bool:
        return key in self.kernels


async def test_observations_distinguish_process_restarts_not_kernel_id() -> (
    None
):
    kernel = Kernel()
    manager = Manager(kernel)
    observer = KernelStateObserver()
    first = await observer.observe(manager, "kernel")
    assert first["alive"] is True
    assert await observer.observe(manager, "kernel") == first
    kernel.provisioner.process = Process()
    restarted = await observer.observe(manager, "kernel")
    assert restarted["kernel_id"] == first["kernel_id"]
    assert restarted["instance_id"] != first["instance_id"]
    gc.collect()
    assert len(observer._instances) == 1
    # Extension/server restart must never reuse the old identity.
    assert (await KernelStateObserver().observe(manager, "kernel"))[
        "instance_id"
    ] != restarted["instance_id"]


async def test_missing_and_dead_process_are_explicit_not_busy_models() -> None:
    kernel = Kernel()
    manager = Manager(kernel)
    observer = KernelStateObserver()
    assert (await observer.observe(manager, "missing"))["alive"] is False
    kernel.alive = False
    assert await observer.observe(manager, "kernel") == {
        "kernel_id": "kernel",
        "alive": False,
        "instance_id": None,
    }


async def test_process_replaced_during_probe_does_not_report_old_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = Kernel()

    async def replace() -> bool:
        kernel.provisioner.process = Process()
        return True

    monkeypatch.setattr(kernel, "is_alive", replace)
    assert (await KernelStateObserver().observe(Manager(kernel), "kernel"))[
        "alive"
    ] is False


async def test_unobservable_live_provisioner_is_not_assumed_safe() -> None:
    kernel = Kernel()
    kernel.provisioner.process = None
    with pytest.raises(RuntimeError, match="continuity is unavailable"):
        await KernelStateObserver().observe(Manager(kernel), "kernel")


async def test_shutdown_before_process_exit_is_already_execution_loss() -> None:
    kernel = Kernel()
    kernel.shutting_down = True
    assert (await KernelStateObserver().observe(Manager(kernel), "kernel"))[
        "alive"
    ] is False
