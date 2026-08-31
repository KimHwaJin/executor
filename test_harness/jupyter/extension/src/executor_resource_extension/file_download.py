"""Bounded reads from one open file, including current-file Range metadata."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
from pathlib import Path
from threading import Event

CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)


class FileRangeError(ValueError):
    def __init__(self, size: int) -> None:
        super().__init__("Requested file range is not satisfiable.")
        self.size = size


class FileChangedError(OSError):
    """The opened file was modified in place, not atomically replaced."""


def parse_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if value is None:
        return 0, size - 1, False
    if size == 0 or not value.startswith("bytes=") or "," in value:
        raise FileRangeError(size)
    start_text, separator, end_text = value[6:].strip().partition("-")
    if not separator or not all(
        not text or (text.isascii() and text.isdigit())
        for text in (start_text, end_text)
    ):
        raise FileRangeError(size)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start, end = max(size - suffix, 0), size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise FileRangeError(size) from exc
    if start >= size or end < start:
        raise FileRangeError(size)
    return start, min(end, size - 1), True


class OpenedFileDownload:
    """Own one descriptor until close; never reopen the path during a read.

    Atomic path replacement leaves this reader on the original file on
    POSIX filesystems. In-place modification is detected on a best-effort
    basis; this is not a filesystem snapshot or a lock on external writers.
    """

    def __init__(
        self,
        path: Path,
        range_header: str | None,
        cancelled: Event | None = None,
    ) -> None:
        self._cancelled = cancelled or Event()
        self._file = path.open("rb")
        try:
            info = os.fstat(self._file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError("Download target is not a regular file.")
            self.size = info.st_size
            self._modified_ns = info.st_mtime_ns
            self.start, self.end, self.partial = parse_range(
                range_header, self.size
            )
            self.length = max(self.end - self.start + 1, 0)
            self._remaining = self.length
            self._sent_digest = hashlib.sha256()
            self._range_digest = hashlib.sha256()
            self.checksum_sha256 = self._hash_opened_file()
            self._file.seek(self.start)
        except BaseException:
            self.close()
            raise

    def _check_unchanged(self) -> None:
        if self._cancelled.is_set():
            raise FileChangedError("Download was cancelled.")
        info = os.fstat(self._file.fileno())
        # Do not compare ctime: unlink/replacement can change the old inode's
        # ctime even though its open descriptor still contains unchanged data.
        if (info.st_size, info.st_mtime_ns) != (
            self.size,
            self._modified_ns,
        ):
            raise FileChangedError(
                "File changed during download: "
                f"opened_size={self.size}, current_size={info.st_size}, "
                f"mtime_changed={info.st_mtime_ns != self._modified_ns}."
            )

    def _hash_opened_file(self) -> str:
        digest = hashlib.sha256()
        offset = 0
        while offset < self.size:
            self._check_unchanged()
            chunk = self._file.read(min(CHUNK_SIZE, self.size - offset))
            if not chunk:
                raise FileChangedError(
                    "File ended during download setup: "
                    f"opened_size={self.size}, read_bytes={offset}, "
                    f"current_size={os.fstat(self._file.fileno()).st_size}."
                )
            digest.update(chunk)
            left = max(self.start - offset, 0)
            right = min(self.end + 1 - offset, len(chunk))
            if left < right:
                self._range_digest.update(chunk[left:right])
            offset += len(chunk)
        self._check_unchanged()
        return digest.hexdigest()

    def read_chunk(self) -> bytes:
        if not self._remaining:
            return b""
        self._check_unchanged()
        chunk = self._file.read(min(CHUNK_SIZE, self._remaining))
        if not chunk:
            raise FileChangedError("File ended during download.")
        self._check_unchanged()
        self._sent_digest.update(chunk)
        self._remaining -= len(chunk)
        if not self._remaining and (
            self._sent_digest.digest() != self._range_digest.digest()
        ):
            # Do not emit the final bytes of a known inconsistent response.
            raise FileChangedError("File content changed during download.")
        return chunk

    def close(self) -> None:
        self._file.close()


async def open_download(
    path: Path, range_header: str | None
) -> OpenedFileDownload:
    """Do not lose an opened descriptor if hashing outlives its HTTP task."""
    for attempt in range(2):
        cancelled = Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                OpenedFileDownload, path, range_header, cancelled
            )
        )
        try:
            return await asyncio.shield(task)
        except FileChangedError as exc:
            # No headers/bytes have escaped yet. A concurrent save or a shared
            # mount's metadata refresh can settle between open and first read.
            # Discard the closed, inconsistent handle and allow ONE new open.
            if attempt == 1:
                raise
            logger.info("Runtime file download setup retry: %s", exc)
        except asyncio.CancelledError:
            cancelled.set()
            task.add_done_callback(_discard_download)
            raise
    raise AssertionError("Download setup did not produce a file or an error.")


def _discard_download(task: asyncio.Task[OpenedFileDownload]) -> None:
    if task.cancelled():
        return
    try:
        task.result().close()
    except Exception:
        # A cancelled setup may itself have failed and closed its descriptor.
        pass
