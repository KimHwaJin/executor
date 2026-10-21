"""Apply the Execution's resource limits, then become the kernel process.

Kernelspecs run ``python -m executor_resource_extension.kernel_launcher
<original kernel argv...>``. The process image is replaced, so no wrapper
process remains and a kernel restart applies the same limits again.
"""

from __future__ import annotations

import os
import resource
import sys
from collections.abc import MutableMapping

MEMORY_LIMIT_ENV = "EXECUTOR_KERNEL_MEMORY_LIMIT_BYTES"
THREADS_ENV = "EXECUTOR_KERNEL_THREADS"
DEFAULT_THREADS = "4"
# BLAS/OpenMP size their pools from the node's cores, not the Pod's CPU
# limit. Each thread reserves virtual memory, which RLIMIT_AS counts.
THREAD_POOL_ENVS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
)


def apply_limits(environ: MutableMapping[str, str]) -> None:
    limit = environ.pop(MEMORY_LIMIT_ENV, None)
    if limit:
        # RLIMIT_AS, not RLIMIT_DATA: kernels before 4.7 ignore mmap
        # allocations in RLIMIT_DATA, and large arrays are mmap'd.
        resource.setrlimit(resource.RLIMIT_AS, (int(limit), int(limit)))
        threads = environ.get(THREADS_ENV, DEFAULT_THREADS)
        for name in THREAD_POOL_ENVS:
            environ.setdefault(name, threads)
        environ.setdefault("MALLOC_ARENA_MAX", "2")
    try:
        # Raising is unprivileged. Under memory pressure the OOM killer then
        # picks a kernel instead of the Jupyter server.
        with open("/proc/self/oom_score_adj", "w") as adj:
            adj.write("1000")
    except OSError:
        pass


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        raise SystemExit("usage: kernel_launcher <kernel command...>")
    apply_limits(os.environ)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
