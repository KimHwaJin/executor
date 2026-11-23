"""Time fsync on the shared result storage, from inside an Executor Pod.

It repeats what Executor does for a result file (write a temporary file,
fsync it, rename it, fsync the directory) and prints how long each part
took. Its files go in a folder of its own, which it removes.

    python scripts/fsync_probe.py DIRECTORY [COUNT=200] [FILE_BYTES=2000]

    kubectl exec -i <pod> -- python - /mnt/data 200 < scripts/fsync_probe.py

Only the standard library is used, so the file can be piped into any
Python.
"""

import os
import sys
import time
import uuid


def _show(label: str, seconds: list[float]) -> None:
    ordered = sorted(seconds)

    def at(fraction: float) -> float:
        index = min(len(ordered) - 1, int(len(ordered) * fraction))
        return ordered[index] * 1000

    print(
        f"{label:<16} p50 {at(0.5):7.2f}ms  p95 {at(0.95):7.2f}ms"
        f"  max {ordered[-1] * 1000:7.2f}ms"
    )


def main() -> None:
    root = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
    folder = os.path.join(root, f".fsync-probe-{uuid.uuid4().hex[:8]}")
    os.makedirs(folder)
    payload = b"x" * size
    file_times: list[float] = []
    directory_times: list[float] = []
    totals: list[float] = []
    try:
        for index in range(count):
            started = time.perf_counter()
            temporary = os.path.join(folder, f"{index}.tmp")
            with open(temporary, "wb") as handle:
                handle.write(payload)
                handle.flush()
                before = time.perf_counter()
                os.fsync(handle.fileno())
                file_times.append(time.perf_counter() - before)
            os.replace(temporary, os.path.join(folder, f"{index}.json"))
            descriptor = os.open(folder, os.O_RDONLY)
            try:
                before = time.perf_counter()
                os.fsync(descriptor)
                directory_times.append(time.perf_counter() - before)
            finally:
                os.close(descriptor)
            totals.append(time.perf_counter() - started)
    finally:
        for name in os.listdir(folder):
            os.remove(os.path.join(folder, name))
        os.rmdir(folder)
    print(f"{root}: {count} files of {size} bytes")
    _show("file fsync", file_times)
    _show("directory fsync", directory_times)
    _show("one file, whole", totals)


if __name__ == "__main__":
    main()
