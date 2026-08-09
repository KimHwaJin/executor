"""Local Jupyter configuration. The token is read from the environment, never logged."""

import os

c = get_config()  # type: ignore[unresolved-reference]  # noqa: F821
c.ServerApp.root_dir = "/workspace/pv"
c.ServerApp.allow_remote_access = True
c.PasswordIdentityProvider.token = os.environ["JUPYTER_TOKEN"]
