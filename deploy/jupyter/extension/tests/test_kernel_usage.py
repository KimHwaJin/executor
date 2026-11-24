import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from executor_resource_extension.kernel_usage import StepUsage, kernel_usages

MIB = 1 << 20

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/clear_refs").exists(), reason="Needs Linux /proc"
)

# A stand-in kernel: each stdin line is one "Step" it runs.
KERNEL = """
import subprocess, sys, time
held = None
for line in sys.stdin:
    command = line.split()
    if command[0] == "alloc":
        held = bytearray(int(command[1]) << 20)
        for i in range(0, len(held), 4096):
            held[i] = 1
    elif command[0] == "free":
        held = None
    elif command[0] == "spin":
        end = time.process_time() + float(command[1])
        while time.process_time() < end:
            pass
    elif command[0] == "child":
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print("done", flush=True)
"""


@pytest.fixture
def kernel() -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [sys.executable, "-c", KERNEL],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    yield process
    for child in _children(process.pid):
        subprocess.run(["kill", str(child)], check=False)
    process.kill()
    process.wait()


def _run(kernel: subprocess.Popen[str], command: str) -> None:
    assert kernel.stdin is not None and kernel.stdout is not None
    kernel.stdin.write(command + "\n")
    kernel.stdin.flush()
    assert kernel.stdout.readline() == "done\n"


def _children(pid: int) -> list[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    return [int(child) for child in path.read_text().split()]


def test_step_peak_covers_only_the_step(
    kernel: subprocess.Popen[str],
) -> None:
    # An earlier Step's peak must not show up in a later one.
    _run(kernel, "alloc 200")
    _run(kernel, "free")

    usage = StepUsage(kernel.pid)
    _run(kernel, "alloc 64")
    _run(kernel, "free")
    step = usage.finish()

    assert 64 * MIB <= step["peak_memory_bytes"] < 200 * MIB
    assert step["cpu_seconds"] >= 0


def test_step_cpu_seconds_counts_kernel_work(
    kernel: subprocess.Popen[str],
) -> None:
    usage = StepUsage(kernel.pid)
    _run(kernel, "spin 0.3")

    assert usage.finish()["cpu_seconds"] >= 0.25


def test_kernel_usage_includes_child_processes(
    kernel: subprocess.Popen[str],
) -> None:
    _run(kernel, "alloc 32")
    alone = kernel_usages({"kernel": kernel.pid})["kernel"]
    _run(kernel, "child")

    usage = kernel_usages({"kernel": kernel.pid})["kernel"]

    assert alone["process_count"] == 1
    assert alone["memory_bytes"] >= 32 * MIB
    assert usage["process_count"] == 2
    assert usage["memory_bytes"] > alone["memory_bytes"]
    assert usage["cpu_seconds"] >= 0


def test_missing_kernel_process_reports_nothing(tmp_path: Path) -> None:
    assert kernel_usages({"gone": None, "ended": 2**22 + 1}) == {
        "gone": {
            "process_count": None,
            "memory_bytes": None,
            "cpu_seconds": None,
        },
        "ended": {
            "process_count": None,
            "memory_bytes": None,
            "cpu_seconds": None,
        },
    }
    # An unreadable /proc reports no values instead of zeros.
    assert StepUsage(1, proc=tmp_path / "no-proc").finish() == {
        "peak_memory_bytes": None,
        "cpu_seconds": None,
    }
