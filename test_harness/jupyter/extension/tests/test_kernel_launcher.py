import json
import os
import subprocess
import sys
from pathlib import Path

import executor_resource_extension

SOURCE_ROOT = Path(executor_resource_extension.__file__).parents[1]
REPORT = """
import json, os, resource
print(json.dumps({
    "as_limit": resource.getrlimit(resource.RLIMIT_AS)[0],
    "env": {k: os.environ.get(k) for k in (
        "EXECUTOR_KERNEL_MEMORY_LIMIT_BYTES", "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "MALLOC_ARENA_MAX",
    )},
    "oom_score_adj": open("/proc/self/oom_score_adj").read().strip(),
}))
"""


def launch(extra_env: dict[str, str]) -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.endswith("_NUM_THREADS") and key != "MALLOC_ARENA_MAX"
    }
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    env.update(extra_env)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "executor_resource_extension.kernel_launcher",
            sys.executable,
            "-c",
            REPORT,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_memory_limit_caps_address_space_and_thread_pools() -> None:
    report = launch(
        {
            "EXECUTOR_KERNEL_MEMORY_LIMIT_BYTES": str(4 * 1024**3),
            "EXECUTOR_KERNEL_THREADS": "2",
        }
    )

    assert report["as_limit"] == 4 * 1024**3
    assert report["env"] == {
        # The kernel's own code does not see the launcher contract.
        "EXECUTOR_KERNEL_MEMORY_LIMIT_BYTES": None,
        "OMP_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
        "MALLOC_ARENA_MAX": "2",
    }
    assert report["oom_score_adj"] == "1000"


def test_explicit_thread_settings_win() -> None:
    report = launch(
        {
            "EXECUTOR_KERNEL_MEMORY_LIMIT_BYTES": str(4 * 1024**3),
            "OMP_NUM_THREADS": "1",
        }
    )

    assert report["env"]["OMP_NUM_THREADS"] == "1"
    assert report["env"]["OPENBLAS_NUM_THREADS"] == "4"


def test_without_memory_limit_only_oom_priority_changes() -> None:
    report = launch({})

    assert report["as_limit"] == -1  # RLIM_INFINITY
    assert set(report["env"].values()) == {None}
    assert report["oom_score_adj"] == "1000"
