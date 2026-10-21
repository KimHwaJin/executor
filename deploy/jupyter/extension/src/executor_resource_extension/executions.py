"""Record kernel executions in journals any Executor replica can follow.

The Jupyter server, not the Executor process, owns the kernel connection.
Each request gets an append-only JSONL journal: a header line, one numbered
line per kernel message for that request, and one numbered terminal line.
An Executor that disappears mid-Step loses nothing; its replacement replays
the journal from the start. Code is sent at most once per request ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from executor_resource_extension.kernel_state import (
    CGROUP_ROOT,
    KernelStateObserver,
    exit_reason,
    memory_limit_events,
)

COMPLETED = "COMPLETED"
LOST = "LOST"
OUTPUT_LIMIT = "OUTPUT_LIMIT"


class JournalNotFoundError(LookupError):
    """No journal exists for the request on this server."""


class KernelNotFoundError(LookupError):
    """The kernel is not running on this server."""


class JournalBusyError(RuntimeError):
    """A running execution's journal cannot be deleted."""


class _Recording:
    def __init__(self) -> None:
        self.changed = asyncio.Condition()
        self.task: asyncio.Task[None] | None = None


class ExecutionJournals:
    def __init__(
        self,
        root: Path,
        kernel_state: KernelStateObserver,
        *,
        max_bytes: int,
        retention_seconds: float,
        liveness_seconds: float = 5,
        # Below the Executor's Jupyter request timeout (30 s by default), so
        # a slow kernel fails the request before the Executor stops waiting
        # instead of receiving code after it.
        ready_timeout_seconds: float = 20,
        cgroup_root: Path = CGROUP_ROOT,
    ) -> None:
        self._root = root
        self._kernel_state = kernel_state
        self._max_bytes = max_bytes
        self._retention_seconds = retention_seconds
        self._liveness_seconds = liveness_seconds
        self._ready_timeout_seconds = ready_timeout_seconds
        self._cgroup_root = cgroup_root
        # Journals written by an earlier server process can never finish.
        self._server_id = uuid4().hex
        self._recordings: dict[str, _Recording] = {}
        self._root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_environment(
        cls, kernel_state: KernelStateObserver
    ) -> ExecutionJournals:
        return cls(
            Path(
                os.environ.get("EXECUTOR_JOURNAL_DIR")
                or Path(tempfile.gettempdir()) / "executor-journals"
            ),
            kernel_state,
            max_bytes=int(
                os.environ.get("EXECUTOR_JOURNAL_MAX_BYTES", 1 << 30)
            ),
            retention_seconds=float(
                os.environ.get("EXECUTOR_JOURNAL_RETENTION_SECONDS", 86400)
            ),
        )

    async def start(
        self, manager: Any, kernel_id: str, request_id: str, code: str
    ) -> bool:
        """Send code once; False means the request was already accepted."""

        path = self._path(request_id)
        if path.exists():
            return False
        if kernel_id not in manager:
            raise KernelNotFoundError(kernel_id)
        self._remove_expired()
        kernel = manager.get_kernel(kernel_id)
        identity = await self._kernel_state.observe(manager, kernel_id)
        if not identity["alive"]:
            raise KernelNotFoundError(kernel_id)
        client = kernel.client()
        client.start_channels()
        try:
            await client.wait_for_ready(timeout=self._ready_timeout_seconds)
            header = _line(
                {
                    "type": "header",
                    "request_id": request_id,
                    "kernel_id": kernel_id,
                    "server_id": self._server_id,
                }
            )
            try:
                # The header exists before code is sent: a missing journal
                # always means the kernel never received this request.
                with path.open("x", encoding="utf-8") as journal:
                    journal.write(header)
            except FileExistsError:
                client.stop_channels()
                return False
            message_id = client.execute(
                code,
                silent=False,
                store_history=True,
                allow_stdin=False,
                stop_on_error=True,
            )
        except BaseException:
            client.stop_channels()
            raise
        recording = _Recording()
        self._recordings[request_id] = recording
        recording.task = asyncio.create_task(
            self._record(
                manager,
                kernel_id,
                identity["instance_id"],
                # Held to read how this process ended once it is replaced.
                getattr(kernel.provisioner, "process", None),
                memory_limit_events(self._cgroup_root),
                client,
                message_id,
                path,
                len(header.encode("utf-8")),
                recording,
            )
        )
        return True

    async def follow(
        self, kernel_id: str, request_id: str, after: int
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield entries after ``after``, a catch-up marker, then live ones."""

        path = self._path(request_id)
        if not path.exists():
            raise JournalNotFoundError(request_id)
        recording = self._recordings.get(request_id)
        # Lines are written whole on the event loop, so a reader never sees a
        # partial line. Binary mode keeps tell() an exact byte offset.
        with path.open("rb") as journal:
            header = json.loads(journal.readline())
            if header["kernel_id"] != kernel_id:
                raise JournalNotFoundError(request_id)
            caught_up = False
            last_seq = 0
            while True:
                line = journal.readline()
                if line:
                    entry = json.loads(line)
                    last_seq = entry["seq"]
                    if last_seq > after:
                        yield entry
                    if entry["type"] == "terminal":
                        if not caught_up:
                            yield _caught_up(last_seq, finished=True)
                        return
                    continue
                if not caught_up:
                    caught_up = True
                    yield _caught_up(last_seq, finished=False)
                if recording is None or recording.task is None:
                    # Recorded by an earlier server process or a recorder
                    # that stopped without finishing: the kernel is gone.
                    recording = self._finish_orphan(
                        path, header, request_id, last_seq
                    )
                    continue
                async with recording.changed:
                    if journal.tell() == path.stat().st_size:
                        await recording.changed.wait()

    def delete(self, kernel_id: str, request_id: str) -> None:
        path = self._path(request_id)
        if request_id in self._recordings:
            raise JournalBusyError(request_id)
        with contextlib.suppress(FileNotFoundError):
            with path.open("rb") as journal:
                if json.loads(journal.readline())["kernel_id"] != kernel_id:
                    raise JournalNotFoundError(request_id)
            path.unlink()

    def _path(self, request_id: str) -> Path:
        return self._root / f"{UUID(request_id)}.jsonl"

    def _finish_orphan(
        self,
        path: Path,
        header: dict[str, Any],
        request_id: str,
        last_seq: int,
    ) -> _Recording:
        recording = self._recordings.get(request_id)
        if recording is not None:
            return recording
        with path.open("a", encoding="utf-8") as journal:
            journal.write(
                _line(
                    _terminal(
                        last_seq + 1,
                        LOST,
                        "The Jupyter server restarted during execution."
                        if header["server_id"] != self._server_id
                        else "Kernel execution recording stopped.",
                    )
                )
            )
        return _Recording()

    async def _record(
        self,
        manager: Any,
        kernel_id: str,
        instance_id: str,
        process: Any,
        memory_events_before: int | None,
        client: Any,
        message_id: str,
        path: Path,
        size: int,
        recording: _Recording,
    ) -> None:
        seq = 0
        reply_received = idle_received = False
        # One handle for the whole recording; flush makes each whole line
        # visible to followers.
        journal = path.open("a", encoding="utf-8")

        async def append(entry: dict[str, Any]) -> None:
            nonlocal seq, size
            seq += 1
            line = _line({"seq": seq, **entry})
            journal.write(line)
            journal.flush()
            size += len(line.encode("utf-8"))
            async with recording.changed:
                recording.changed.notify_all()

        iopub = asyncio.ensure_future(client.get_iopub_msg())
        shell = asyncio.ensure_future(client.get_shell_msg())
        try:
            while not (reply_received and idle_received):
                done, _ = await asyncio.wait(
                    {iopub, shell},
                    timeout=self._liveness_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    current = await self._kernel_state.observe(
                        manager, kernel_id
                    )
                    if current["instance_id"] != instance_id:
                        await append(
                            _terminal_entry(
                                LOST,
                                "The kernel process stopped or restarted "
                                "during execution."
                                + exit_reason(
                                    process,
                                    memory_events_before,
                                    memory_limit_events(self._cgroup_root),
                                ),
                            )
                        )
                        return
                    continue
                for future, channel in ((shell, "shell"), (iopub, "iopub")):
                    if future not in done:
                        continue
                    message = future.result()
                    if channel == "shell":
                        shell = asyncio.ensure_future(client.get_shell_msg())
                    else:
                        iopub = asyncio.ensure_future(client.get_iopub_msg())
                    if message["parent_header"].get("msg_id") != message_id:
                        continue
                    msg_type = message["header"]["msg_type"]
                    content = message["content"]
                    if channel == "shell" and msg_type == "execute_reply":
                        reply_received = True
                    elif msg_type == "status":
                        idle_received = content["execution_state"] == "idle"
                        continue
                    if size > self._max_bytes:
                        await append(
                            _terminal_entry(
                                OUTPUT_LIMIT,
                                f"Execution output exceeded the "
                                f"{self._max_bytes}-byte journal limit.",
                            )
                        )
                        return
                    await append(
                        {
                            "type": "message",
                            "channel": channel,
                            "msg_type": msg_type,
                            "content": content,
                        }
                    )
            await append(_terminal_entry(COMPLETED))
        except asyncio.CancelledError:
            # The server is stopping: end the journal so followers return.
            await append(
                _terminal_entry(LOST, "Kernel execution recording stopped.")
            )
            raise
        except Exception as exc:
            await append(
                _terminal_entry(
                    LOST,
                    f"Kernel execution recording failed: {type(exc).__name__}.",
                )
            )
        finally:
            journal.close()
            iopub.cancel()
            shell.cancel()
            client.stop_channels()
            self._recordings.pop(path.stem, None)
            async with recording.changed:
                recording.changed.notify_all()

    def _remove_expired(self) -> None:
        cutoff = time.time() - self._retention_seconds
        for path in self._root.glob("*.jsonl"):
            if path.stem in self._recordings:
                continue
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()


def _line(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"


def _terminal_entry(state: str, message: str | None = None) -> dict[str, Any]:
    return {"type": "terminal", "state": state, "message": message}


def _terminal(seq: int, state: str, message: str) -> dict[str, Any]:
    return {"seq": seq, **_terminal_entry(state, message)}


def _caught_up(seq: int, *, finished: bool) -> dict[str, Any]:
    return {"type": "caught_up", "seq": seq, "finished": finished}
