"""Local Jupyter configuration. The token is read from the environment, never logged."""

import os

c = get_config()  # type: ignore[unresolved-reference]  # noqa: F821
c.ServerApp.root_dir = os.getenv("JUPYTER_ROOT_DIR", "/workspace/pv")
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.port = 8888
c.ServerApp.open_browser = False
c.ServerApp.allow_remote_access = True
c.PasswordIdentityProvider.token = os.environ["JUPYTER_TOKEN"]
c.KernelSpecManager.allowed_kernelspecs = {"basic", "ml"}
c.MappingKernelManager.default_kernel_name = "basic"
