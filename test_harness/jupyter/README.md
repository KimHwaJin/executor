# Executor Jupyter Test Harness

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

Run the build from the repository root because the Dockerfile uses the repository as its build
context:

```bash
docker build --tag executor-jupyter:local --file test_harness/jupyter/Dockerfile .
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
Changing `HOME` does not change the contents root. Executor does not mount or need this physical
root; it uses Runtime-relative paths through authenticated Jupyter APIs.

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

## Runtime storage endpoints

The same authenticated extension prepares workspaces, snapshots artifacts, computes file metadata
and SHA-256 on the Jupyter side, and reads append-only manifests. Notebook read/write uses
Jupyter's standard Contents API. Executor can therefore persist paths and metadata without
mounting Jupyter shared storage. File scans and hashing run in a worker thread so they do not block
Jupyter's server event loop; Executor applies `JUPYTER_STORAGE_TIMEOUT_SECONDS` (default 300) to
these potentially slower calls.

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

## Native installation without Docker

The repository also provides one Python entry point for Linux, macOS, and Windows PowerShell. It
uses uv-managed CPython, creates three isolated environments under `test_harness/jupyter/.native`, installs the
same requirements and Executor extension as the image, and exposes only the `basic` and `ml`
kernels. WSL is not required on Windows.

Prerequisites:

- `uv` available on `PATH`;
- network access to the configured Python package index during setup;
- 64-bit Windows when using the supplied ML binary wheels;
- Microsoft Visual C++ Redistributable on Windows for native ML libraries;
- `libomp` on macOS (`brew install libomp`) for XGBoost and LightGBM;
- `libgomp` on Linux, normally provided by `libgomp1` or the equivalent distribution package.

The setup command is the same in POSIX shells and PowerShell. From the repository root:

```text
uv run python test_harness/jupyter/native.py setup
```

The command installs uv-managed Python 3.11 and 3.12 when they are absent. It does not alter the
system Python installation. Re-run it after changing a requirements file or the Jupyter extension.

Set a token in the current shell and start the server. POSIX:

```bash
export JUPYTER_TOKEN='replace-with-a-local-secret'
uv run python test_harness/jupyter/native.py run \
  --root-dir ./test_harness/jupyter/workspace \
  --host 127.0.0.1 \
  --port 8888
```

PowerShell:

```powershell
$env:JUPYTER_TOKEN = 'replace-with-a-local-secret'
uv run python test_harness/jupyter/native.py run `
  --root-dir .\test_harness\jupyter\workspace `
  --host 127.0.0.1 `
  --port 8888
```

The token remains an environment value and is not written into Jupyter configuration. Prefer the
environment variable over `--token`, because command-line arguments may be visible in local process
inspection.

In a second shell, use the same token and verify standard status, exact kernelspecs, resource
observation, workspace creation, and Artifact snapshot endpoints:

```text
uv run python test_harness/jupyter/native.py verify --endpoint http://127.0.0.1:8888
```

Use `--` to pass additional JupyterLab options to `run`, for example:

```text
uv run python test_harness/jupyter/native.py run --port 8888 -- --ServerApp.base_url=/jupyter
```

On Linux, the runner resolves the current process's cgroup v2 leaf from `/proc/self/cgroup`. On
Windows and macOS, or when cgroup files are unavailable, resource usage and utilization are null
with safe error codes. Configure capacity explicitly when desired:

```text
uv run python test_harness/jupyter/native.py run --cpu-cores 4 --memory-bytes 8589934592
```

Missing usage measurements do not make the Jupyter target unhealthy. Executor falls back to its
configured slot-based scheduling when every candidate lacks a fresh resource observation.

## Harness tests

From the repository root, the native runner helpers are included in the Executor test command:

```text
uv run pytest -q
```

The server extension has its own dependency boundary and test environment:

```text
uv run --project test_harness/jupyter/extension --with pytest \
  pytest -q test_harness/jupyter/extension/tests
```
