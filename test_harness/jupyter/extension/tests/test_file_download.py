import asyncio
import hashlib
import os
from pathlib import Path
from threading import Event

import pytest
from executor_resource_extension.file_download import (
    CHUNK_SIZE,
    FileChangedError,
    FileRangeError,
    OpenedFileDownload,
    open_download,
    parse_range,
)


def read_all(download: OpenedFileDownload) -> bytes:
    return b"".join(iter(download.read_chunk, b""))


@pytest.mark.parametrize(
    "content", [b"", b"hello", b"x" * (CHUNK_SIZE * 3 + 7)]
)
def test_full_file_and_checksum(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    download = OpenedFileDownload(path, None)
    try:
        assert download.size == download.length == len(content)
        assert not download.partial
        assert download.checksum_sha256 == hashlib.sha256(content).hexdigest()
        assert read_all(download) == content
    finally:
        download.close()
    assert download._file.closed


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "execution.ipynb"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, (0, 9, False)),
        ("bytes=2-5", (2, 5, True)),
        ("bytes=7-", (7, 9, True)),
        ("bytes=-3", (7, 9, True)),
        ("bytes=-99", (0, 9, True)),
        ("bytes=0-99", (0, 9, True)),
        ("bytes=0-0", (0, 0, True)),
    ],
)
def test_ranges(header: str | None, expected: tuple[int, int, bool]) -> None:
    assert parse_range(header, 10) == expected


@pytest.mark.parametrize(
    "header",
    [
        "",
        "bytes=",
        "bytes=-",
        "bytes=-0",
        "bytes=-1-2",
        "bytes=9-2",
        "bytes=10-",
        "bytes=0-1,4-5",
        "items=0-2",
        "bytes=+1-2",
        "bytes=\uff11-\uff12",
    ],
)
def test_invalid_range_closes_file(path: Path, header: str) -> None:
    path.write_bytes(b"0123456789")
    with pytest.raises(FileRangeError) as caught:
        OpenedFileDownload(path, header)
    assert caught.value.size == 10


def test_five_ranges_reassemble_exact_original(path: Path) -> None:
    content = bytes(range(256)) * 10_000
    path.write_bytes(content)
    assembled = b""
    part_size = len(content) // 5
    for index in range(5):
        start = index * part_size
        end = (index + 1) * part_size - 1
        opened = OpenedFileDownload(path, f"bytes={start}-{end}")
        try:
            part = read_all(opened)
            assert part == content[start : end + 1]
            assert (
                opened.checksum_sha256 == hashlib.sha256(content).hexdigest()
            )
            assembled += part
        finally:
            opened.close()
    assert assembled == content


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX open-inode replacement semantics"
)
def test_atomic_replacement_keeps_original_open_file(path: Path) -> None:
    original = b"old" * CHUNK_SIZE
    path.write_bytes(original)
    opened = OpenedFileDownload(path, None)
    replacement = path.with_suffix(".new")
    replacement.write_bytes(b"new notebook")
    try:
        first = opened.read_chunk()
        os.replace(replacement, path)
        assert first + read_all(opened) == original
        assert opened.checksum_sha256 == hashlib.sha256(original).hexdigest()
        latest = OpenedFileDownload(path, None)
        try:
            assert read_all(latest) == b"new notebook"
        finally:
            latest.close()
    finally:
        opened.close()


@pytest.mark.parametrize(
    "replacement",
    [b"", b"smaller", b"same" * CHUNK_SIZE, b"larger" * CHUNK_SIZE],
)
def test_in_place_edit_is_not_reported_as_success(
    path: Path, replacement: bytes
) -> None:
    path.write_bytes(b"orig" * CHUNK_SIZE)
    opened = OpenedFileDownload(path, None)
    try:
        assert opened.read_chunk()
        path.write_bytes(replacement)
        with pytest.raises(FileChangedError):
            read_all(opened)
    finally:
        opened.close()


def test_empty_file_range_fails_but_full_succeeds(path: Path) -> None:
    path.write_bytes(b"")
    with pytest.raises(FileRangeError) as caught:
        OpenedFileDownload(path, "bytes=0-0")
    assert caught.value.size == 0


def test_same_size_edit_with_restored_mtime_fails_hash_check(
    path: Path,
) -> None:
    path.write_bytes(b"a" * CHUNK_SIZE * 3)
    opened = OpenedFileDownload(path, None)
    try:
        assert opened.read_chunk()
        info = path.stat()
        with path.open("r+b") as writer:
            writer.seek(CHUNK_SIZE * 2)
            writer.write(b"b" * CHUNK_SIZE)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with pytest.raises(FileChangedError, match="content changed"):
            read_all(opened)
    finally:
        opened.close()


async def test_cancelled_setup_releases_descriptor_when_hash_thread_finishes(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await asyncio.to_thread(path.write_bytes, b"original")
    started, release, finished = Event(), Event(), Event()
    opened_files: list[OpenedFileDownload] = []
    original = OpenedFileDownload._hash_opened_file

    def slow_hash(self: OpenedFileDownload) -> str:
        opened_files.append(self)
        started.set()
        release.wait(5)
        return original(self)

    original_close = OpenedFileDownload.close

    def close(self: OpenedFileDownload) -> None:
        original_close(self)
        finished.set()

    monkeypatch.setattr(OpenedFileDownload, "_hash_opened_file", slow_hash)
    monkeypatch.setattr(OpenedFileDownload, "close", close)
    task = asyncio.create_task(open_download(path, None))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 2)
    assert opened_files[0]._file.closed


@pytest.mark.parametrize("keep_changing", [False, True])
async def test_setup_reopens_once_only_before_headers(
    path: Path, monkeypatch: pytest.MonkeyPatch, keep_changing: bool
) -> None:
    await asyncio.to_thread(path.write_bytes, b"old")
    opened_files: list[OpenedFileDownload] = []
    original = OpenedFileDownload._hash_opened_file

    def changing_hash(self: OpenedFileDownload) -> str:
        opened_files.append(self)
        if len(opened_files) == 1 or keep_changing:
            path.write_bytes(
                b"" if len(opened_files) == 1 else b"changed again"
            )
        return original(self)

    monkeypatch.setattr(OpenedFileDownload, "_hash_opened_file", changing_hash)
    if keep_changing:
        with pytest.raises(FileChangedError):
            await open_download(path, None)
    else:
        opened = await open_download(path, None)
        try:
            assert opened.size == 0
            assert opened.read_chunk() == b""
        finally:
            opened.close()
    assert len(opened_files) == 2
    assert all(opened._file.closed for opened in opened_files)
