# Executor Jupyter Image

This directory owns the JupyterLab image used by Executor Runtime Targets. Image build,
kernel-package maintenance, authentication, workspace mounting, and the resource observation
extension are documented here rather than in the Executor service README.

## Image layout

The image is built from `python:3.12-slim-bookworm` and runs JupyterLab as the non-root `jovyan`
user (UID/GID 1000). Three isolated virtual environments are created:

| Environment | Python | Purpose | Package list |
| --- | --- | --- | --- |
| Jupyter server | 3.12 | JupyterLab and server process | `environments/server/requirements.txt` |
| `basic` kernel | 3.11 | General data analysis | `environments/basic/requirements.txt` |
| `ml` kernel | 3.12 | Data analysis plus ML libraries | `environments/ml/requirements.txt` |

Only the `basic` and `ml` kernelspecs are exposed. The default kernel is `basic`. The ML package
list includes the Basic package list; keep that relationship when updating dependencies.

## Build

Run the build from the repository root because the Dockerfile copies both this directory and the
sibling `docker/jupyter_resource_extension` package:

```bash
docker build --tag executor-jupyter:local --file docker/jupyter/Dockerfile .
```

For the local Compose topology:

```bash
docker compose build jupyter
```

Rebuild the image after changing a requirements file, server configuration, startup script, or
resource extension. The image intentionally has no separate build-time validation stage.

## Runtime configuration

The image has two primary runtime settings:

- `JUPYTER_TOKEN` is required and has no image default. Inject it through the deployment secret.
- `JUPYTER_ROOT_DIR` is the Jupyter contents root and defaults to `/workspace/pv`.

Mount the shared workspace at the same in-container path configured by `JUPYTER_ROOT_DIR`:

```bash
docker run --detach --publish 8888:8888 \
  --env JUPYTER_ROOT_DIR=/workspace/pv \
  --env JUPYTER_TOKEN="${JUPYTER_TOKEN}" \
  --volume /host/workspace:/workspace/pv \
  executor-jupyter:local
```

`HOME` is `/home/jovyan`; it stores user and Jupyter configuration, not execution workspaces.
Changing `HOME` does not change the contents root. Executor and Jupyter must instead agree on the
runtime workspace root, which is `/workspace/pv` in the provided Compose configuration.

## Resource endpoint

The authenticated server extension exposes aggregate container resource observations without
starting a monitoring kernel:

```bash
curl --fail \
  --header "Authorization: token ${JUPYTER_TOKEN}" \
  http://127.0.0.1:8888/executor/resource-status
```

It reads cgroup v2 `cpu.stat`, `cpu.max`, `memory.current`, `memory.max`, and `cgroup.procs`.
`JUPYTER_RESOURCE_CPU_CORES` and `JUPYTER_RESOURCE_MEMORY_BYTES` provide capacity when the cgroup
has no readable finite limit. If a usage file is unavailable, that resource's usage and
utilization are null and a safe error code explains why; there is no secondary measurement source.
The response contains only aggregate values and never returns process command lines, environment
variables, or credentials.

## Verification

After starting the image, verify the server, kernelspecs, and resource endpoint:

```bash
curl --fail \
  --header "Authorization: token ${JUPYTER_TOKEN}" \
  http://127.0.0.1:8888/api/status

curl --fail \
  --header "Authorization: token ${JUPYTER_TOKEN}" \
  http://127.0.0.1:8888/api/kernelspecs

curl --fail \
  --header "Authorization: token ${JUPYTER_TOKEN}" \
  http://127.0.0.1:8888/executor/resource-status
```

The kernelspec response must advertise `basic` and `ml` only. Runtime Target registration and
scheduling remain Executor service concerns and are documented in the repository root README.
