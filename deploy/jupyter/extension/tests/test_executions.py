import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from executor_resource_extension.executions import (
    ExecutionJournals,
    JournalBusyError,
    JournalNotFoundError,
    KernelNotFoundError,
)


class Client:
    def __init__(self) -> None:
        self.iopub: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.shell: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[str] = []
        self.stopped = False

    def start_channels(self) -> None:
        pass

    def stop_channels(self) -> None:
        self.stopped = True

    async def wait_for_ready(self, timeout: float) -> None:  # noqa: ASYNC109
        pass

    def execute(self, code: str, **_kwargs: Any) -> str:
        self.sent.append(code)
        return "msg"

    async def get_iopub_msg(self) -> dict[str, Any]:
        return await self.iopub.get()

    async def get_shell_msg(self) -> dict[str, Any]:
        return await self.shell.get()

    def output(self, text: str, parent: str = "msg") -> None:
        self.iopub.put_nowait(
            _message(parent, "stream", {"name": "stdout", "text": text})
        )

    def finish(self) -> None:
        self.shell.put_nowait(
            _message("msg", "execute_reply", {"status": "ok"})
        )
        self.iopub.put_nowait(
            _message("msg", "status", {"execution_state": "idle"})
        )


class Process:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class Provisioner:
    def __init__(self) -> None:
        self.process = Process()


class Kernel:
    def __init__(self, client: Client) -> None:
        self._client = client
        self.provisioner = Provisioner()

    def client(self) -> Client:
        return self._client


class Manager:
    def __init__(self, client: Client) -> None:
        self.kernels = {"kernel": Kernel(client)}

    def __contains__(self, key: str) -> bool:
        return key in self.kernels

    def get_kernel(self, key: str) -> Kernel:
        return self.kernels[key]


class KernelState:
    def __init__(self) -> None:
        self.instance_id: str | None = "first"

    async def observe(self, _manager: Any, kernel_id: str) -> dict[str, Any]:
        return {
            "kernel_id": kernel_id,
            "alive": self.instance_id is not None,
            "instance_id": self.instance_id,
        }


def _message(
    parent: str, msg_type: str, content: dict[str, Any]
) -> dict[str, Any]:
    return {
        "parent_header": {"msg_id": parent},
        "header": {"msg_type": msg_type},
        "content": content,
    }


def _journals(
    root: Path, state: KernelState | None = None, **kwargs: Any
) -> ExecutionJournals:
    return ExecutionJournals(
        root,
        state or KernelState(),  # ty: ignore[invalid-argument-type]
        max_bytes=kwargs.get("max_bytes", 1 << 20),
        retention_seconds=3600,
        liveness_seconds=kwargs.get("liveness_seconds", 5),
        cgroup_root=kwargs.get("cgroup_root", root / "no-cgroup"),
    )


async def _collect(
    journals: ExecutionJournals, request_id: str, after: int = 0
) -> list[dict[str, Any]]:
    entries = []
    async with asyncio.timeout(5):
        async for entry in journals.follow("kernel", request_id, after):
            entries.append(entry)
    return entries


async def test_code_is_sent_once_and_any_follower_replays_everything(
    tmp_path: Path,
) -> None:
    client = Client()
    manager = Manager(client)
    journals = _journals(tmp_path)
    request_id = str(uuid4())

    assert await journals.start(manager, "kernel", request_id, "print(1)")
    assert not await journals.start(manager, "kernel", request_id, "print(1)")
    assert client.sent == ["print(1)"]

    live = asyncio.create_task(_collect(journals, request_id))
    client.output("other request", parent="unrelated")
    client.output("one\n")
    client.output("two\n")
    client.finish()
    entries = await live

    assert [entry["type"] for entry in entries] == [
        "caught_up",
        "message",
        "message",
        "message",
        "terminal",
    ]
    assert [entry.get("seq") for entry in entries[1:]] == [1, 2, 3, 4]
    assert entries[-1]["state"] == "COMPLETED"
    # A kernel that finished cleanly keeps its channels for the next Step.
    assert not client.stopped

    replay = await _collect(journals, request_id)
    assert [entry["type"] for entry in replay][-2:] == [
        "terminal",
        "caught_up",
    ]
    assert replay[-1] == {"type": "caught_up", "seq": 4, "finished": True}
    resumed = await _collect(journals, request_id, after=2)
    assert [entry.get("seq") for entry in resumed] == [3, 4, 4]


async def test_completed_journal_reports_the_step_usage(
    tmp_path: Path,
) -> None:
    client = Client()
    manager = Manager(client)
    # This test process stands in for the kernel's process.
    manager.kernels["kernel"].provisioner.process.pid = os.getpid()
    journals = _journals(tmp_path)
    request_id = str(uuid4())

    await journals.start(manager, "kernel", request_id, "pass")
    client.finish()
    [terminal] = [
        entry
        for entry in await _collect(journals, request_id)
        if entry["type"] == "terminal"
    ]

    assert terminal["state"] == "COMPLETED"
    assert terminal["usage"]["peak_memory_bytes"] > 0
    assert terminal["usage"]["cpu_seconds"] >= 0


async def test_stopped_kernel_finishes_the_journal_as_lost(
    tmp_path: Path,
) -> None:
    client = Client()
    state = KernelState()
    journals = _journals(tmp_path, state, liveness_seconds=0.01)
    request_id = str(uuid4())
    await journals.start(Manager(client), "kernel", request_id, "work()")
    state.instance_id = None

    entries = await _collect(journals, request_id)

    assert entries[-1]["state"] == "LOST"
    with pytest.raises(KernelNotFoundError):
        await journals.start(Manager(client), "kernel", str(uuid4()), "x")


@pytest.mark.parametrize(
    ("returncode", "failcnt_after", "reason"),
    [
        (
            -9,
            "8",
            " It was killed by SIGKILL. The container reached its memory "
            "limit during execution, so it was most likely killed for "
            "running out of memory.",
        ),
        (-9, "5", " It was killed by SIGKILL."),
        (1, "8", " It exited with code 1."),
        (None, "8", ""),
    ],
)
async def test_lost_kernel_reason_names_its_exit_and_memory_limit(
    tmp_path: Path,
    returncode: int | None,
    failcnt_after: str,
    reason: str,
) -> None:
    failcnt = tmp_path / "cgroup" / "memory" / "memory.failcnt"
    failcnt.parent.mkdir(parents=True)
    failcnt.write_text("5\n")
    client = Client()
    state = KernelState()
    journals = _journals(
        tmp_path / "journals",
        state,
        liveness_seconds=0.01,
        cgroup_root=tmp_path / "cgroup",
    )
    request_id = str(uuid4())
    manager = Manager(client)
    await journals.start(manager, "kernel", request_id, "work()")
    failcnt.write_text(f"{failcnt_after}\n")
    manager.kernels["kernel"].provisioner.process.returncode = returncode
    state.instance_id = None

    entries = await _collect(journals, request_id)

    assert entries[-1]["state"] == "LOST"
    assert entries[-1]["message"] == (
        "The kernel process stopped or restarted during execution." + reason
    )


async def test_output_beyond_the_journal_limit_stops_recording(
    tmp_path: Path,
) -> None:
    client = Client()
    journals = _journals(tmp_path, max_bytes=400)
    request_id = str(uuid4())
    await journals.start(Manager(client), "kernel", request_id, "spam()")
    for _ in range(10):
        client.output("x" * 100)

    entries = await _collect(journals, request_id)

    assert entries[-1]["state"] == "OUTPUT_LIMIT"
    assert sum(entry["type"] == "message" for entry in entries) < 10


async def test_journal_left_by_an_earlier_server_reads_as_lost(
    tmp_path: Path,
) -> None:
    request_id = str(uuid4())
    await _journals(tmp_path).start(
        Manager(Client()), "kernel", request_id, "work()"
    )

    restarted = _journals(tmp_path)
    entries = await _collect(restarted, request_id)

    assert entries[-1]["state"] == "LOST"
    assert "restarted" in entries[-1]["message"]
    lines = (tmp_path / f"{request_id}.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["state"] == "LOST"


async def test_journals_are_deleted_only_when_finished(
    tmp_path: Path,
) -> None:
    client = Client()
    journals = _journals(tmp_path)
    request_id = str(uuid4())
    with pytest.raises(JournalNotFoundError):
        await anext(journals.follow("kernel", request_id, 0))
    await journals.start(Manager(client), "kernel", request_id, "x")
    with pytest.raises(JournalBusyError):
        journals.delete("kernel", request_id)
    with pytest.raises(JournalNotFoundError):
        await anext(journals.follow("other-kernel", request_id, 0))

    client.finish()
    await _collect(journals, request_id)
    journals.delete("kernel", request_id)
    journals.delete("kernel", request_id)

    with pytest.raises(JournalNotFoundError):
        await anext(journals.follow("kernel", request_id, 0))


async def test_stopped_recording_releases_its_followers(
    tmp_path: Path,
) -> None:
    client = Client()
    journals = _journals(tmp_path)
    request_id = str(uuid4())
    await journals.start(Manager(client), "kernel", request_id, "work()")
    follower = asyncio.create_task(_collect(journals, request_id))
    await asyncio.sleep(0)

    # The server stopping cancels the recorder mid-execution.
    recorder = journals._recordings[request_id].task
    assert recorder is not None
    recorder.cancel()
    entries = await follower

    assert entries[-1]["state"] == "LOST"
    assert client.stopped


class FreshClientKernel(Kernel):
    """Hands out a new client each time, like a real kernel manager."""

    def __init__(self) -> None:
        super().__init__(Client())
        self.made: list[Client] = []

    def client(self) -> Client:
        self.made.append(Client())
        return self.made[-1]


async def _run_to_completion(
    journals: ExecutionJournals, manager: Manager, kernel: FreshClientKernel
) -> Client:
    request_id = str(uuid4())
    assert await journals.start(manager, "kernel", request_id, "pass")
    client = kernel.made[-1]
    client.finish()
    assert (await _collect(journals, request_id))[-1]["state"] == "COMPLETED"
    return client


async def test_kernel_client_is_kept_between_requests(tmp_path: Path) -> None:
    kernel = FreshClientKernel()
    manager = Manager(Client())
    manager.kernels["kernel"] = kernel
    journals = _journals(tmp_path)

    first = await _run_to_completion(journals, manager, kernel)
    second = await _run_to_completion(journals, manager, kernel)

    assert second is first and len(kernel.made) == 1
    assert first.sent == ["pass", "pass"]
    assert not first.stopped


async def test_replaced_kernel_process_gets_a_new_client(
    tmp_path: Path,
) -> None:
    kernel = FreshClientKernel()
    manager = Manager(Client())
    manager.kernels["kernel"] = kernel
    state = KernelState()
    journals = _journals(tmp_path, state)

    first = await _run_to_completion(journals, manager, kernel)
    state.instance_id = "second"
    second = await _run_to_completion(journals, manager, kernel)

    assert second is not first
    assert first.stopped and not second.stopped


async def test_request_during_a_recording_gets_its_own_client(
    tmp_path: Path,
) -> None:
    kernel = FreshClientKernel()
    manager = Manager(Client())
    manager.kernels["kernel"] = kernel
    journals = _journals(tmp_path)
    running, overlapping = str(uuid4()), str(uuid4())

    assert await journals.start(manager, "kernel", running, "slow")
    assert await journals.start(manager, "kernel", overlapping, "fast")
    kept, own = kernel.made
    own.finish()
    assert (await _collect(journals, overlapping))[-1]["state"] == "COMPLETED"
    assert own.stopped and not kept.stopped
    kept.finish()
    assert (await _collect(journals, running))[-1]["state"] == "COMPLETED"

    again = str(uuid4())
    assert await journals.start(manager, "kernel", again, "next")
    assert kept.sent == ["slow", "next"] and len(kernel.made) == 2
    kept.finish()
    assert (await _collect(journals, again))[-1]["state"] == "COMPLETED"


async def test_client_of_a_lost_or_removed_kernel_is_closed(
    tmp_path: Path,
) -> None:
    kernel = FreshClientKernel()
    manager = Manager(Client())
    manager.kernels["kernel"] = kernel
    state = KernelState()
    journals = _journals(tmp_path, state, liveness_seconds=0.01)

    request_id = str(uuid4())
    assert await journals.start(manager, "kernel", request_id, "crash")
    state.instance_id = "restarted"
    assert (await _collect(journals, request_id))[-1]["state"] == "LOST"
    assert kernel.made[0].stopped

    kept = await _run_to_completion(journals, manager, kernel)
    manager.kernels["other"] = FreshClientKernel()
    del manager.kernels["kernel"]
    other = str(uuid4())
    assert await journals.start(manager, "other", other, "pass")
    assert kept.stopped
