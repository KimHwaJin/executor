"""Jupyter image configuration populated only from runtime environment variables."""

import os

root_dir = os.environ["JUPYTER_ROOT_DIR"]
token = os.environ["JUPYTER_TOKEN"]

c = get_config()  # type: ignore[unresolved-reference]  # noqa: F821
c.ServerApp.root_dir = root_dir
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.port = 8888
c.ServerApp.open_browser = False
c.ServerApp.allow_remote_access = True
c.PasswordIdentityProvider.token = token
c.KernelSpecManager.allowed_kernelspecs = {"basic", "ml"}
c.MappingKernelManager.default_kernel_name = "basic"
