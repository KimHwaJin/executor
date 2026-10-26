"""Jupyter image configuration; deployment values come from runtime environment variables."""

import os
import sys
from pathlib import Path

root_dir = os.environ["JUPYTER_ROOT_DIR"]
token = os.environ["JUPYTER_TOKEN"]

c = get_config()  # type: ignore[unresolved-reference]  # noqa: F821
c.ServerApp.root_dir = root_dir
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.port = 8888
c.ServerApp.open_browser = False
c.ServerApp.allow_remote_access = True
c.LabServerApp.notebook_starts_kernel = False
c.PasswordIdentityProvider.token = token
# Only the kernelspecs registered into the server environment at build time.
kernelspecs = Path(sys.prefix, "share", "jupyter", "kernels")
c.KernelSpecManager.allowed_kernelspecs = {
    path.name for path in kernelspecs.iterdir()
}
c.MappingKernelManager.default_kernel_name = "default"
