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
and SHA-256 on the Jupyter side, and reads append-only manifests. Notebook reads and explicit
Agent-authored file writes use Jupyter's standard Contents API. Executed notebook materialization
uses the Output Journal endpoint described below, so Executor does not download and re-upload all
cell outputs. Executor can therefore persist paths and metadata without mounting Jupyter shared
storage. File scans and hashing run in a worker thread so they do not block Jupyter's server event
loop; Executor applies `JUPYTER_STORAGE_TIMEOUT_SECONDS` (default 300) to these potentially slower
calls.

The extension also exposes authenticated internal Output Journal operations:

- `POST /executor/storage/notebooks/prepare`
- `POST /executor/storage/output-journals/begin`
- `POST /executor/storage/output-journals/append`
- `POST /executor/storage/output-journals/finalize`
- `POST /executor/storage/output-journals/abort`
- `POST /executor/storage/output-journals/materialize-notebook`

These are Runtime-driver endpoints, not public Agent APIs. They durably store complete Step output
under `<workspace>/outputs/<operation>/<step>/<attempt>/<fencing-token>/`. Append uses both a stable
UUID `batch_id` and `expected_offset`: an identical replay is idempotent, while a reused batch ID
with changed records or a non-current offset returns HTTP 409. Output bodies are stored through
one append-only `journal.jsonl` per Step Attempt. Text and structured output stay in that journal;
`image/*` output is stored as native files under `images/`. Responses contain checksums and opaque
`journal://` references rather than echoing bodies or physical paths. All operations require the
same Jupyter token as the standard Contents API, and the workspace must first be created through
`workspaces/prepare`.

`notebooks/prepare` atomically creates or appends stable, Executor-managed code
cells before their Operation executes. It is intentionally implemented without
NbModelClient, YDoc, or RTC, so an already open JupyterLab document may require
reload. `begin` also stores the exact Step source in the JSONL header. After
terminal Journals are selected by Executor fencing metadata,
`materialize-notebook` reconstructs complete Jupyter outputs from JSONL content
and native image files and updates the matching prepared cells without removing
pending cells. The request carries only ordered Journal identities and execution counts,
not accumulated source or output bodies.

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

### Windows installation through an internal Nexus

The default setup command can download managed Python distributions and packages from public
repositories. For a closed Windows network, distribute `uv.exe` and the official 64-bit Python
3.11 and 3.12 installers through an approved internal channel, install both Python versions, and
publish all packages required by `environments/server`, `environments/basic`, and
`environments/ml` to a Nexus PyPI repository.

Resolve the installed Python executables and run the bootstrap script directly with Python 3.12.
Supplying both executable paths prevents `native.py` from running `uv python install`, while
`--index-url` replaces the public PyPI default for every package installation:

```powershell
$Python311 = py -3.11 -c "import sys; print(sys.executable)"
$Python312 = py -3.12 -c "import sys; print(sys.executable)"
$NexusIndex = "https://nexus.example/repository/pypi-group/simple"

# Use the Windows certificate store when Nexus is signed by an internal corporate CA.
$env:UV_SYSTEM_CERTS = "true"

& $Python312 test_harness\jupyter\native.py setup `
  --python-311 $Python311 `
  --python-312 $Python312 `
  --index-url $NexusIndex
```

Do not put Nexus credentials in the repository or in this command. Use the authentication method
approved for the internal Nexus installation. Do not set `UV_OFFLINE=true` when Nexus must remain
reachable: uv offline mode disables Nexus access as well. If Nexus has no upstream access, preload
its hosted PyPI repository with Windows x64 wheels and their transitive dependencies before setup.

### Windows REST diagnostics

After starting one or more native Jupyter servers, use the PowerShell diagnostic to validate every
REST endpoint required by the Executor Worker. The default checks ports 8888, 8889, and 8890 with
the token from `JUPYTER_TOKEN`:

```powershell
$env:JUPYTER_TOKEN = "local-test-token"
.\test_harness\jupyter\scripts\windows_rest_diagnostics.ps1
```

The script checks status, kernelspecs, resource observation, workspace preparation, Artifact
snapshots, kernel creation/get/interrupt/deletion, notebook write/read, Jupyter checkpoint creation,
file metadata, and manifest reads. It uses the same long
`users/.../projects/.../sessions/.../executions/...` hierarchy as a real Execution, prints the
relative checkpoint path length to expose Windows path-length failures, and deletes the unique
workspace afterward. Keep the workspace for inspection or test a subset of endpoints with:

```powershell
.\test_harness\jupyter\scripts\windows_rest_diagnostics.ps1 `
  -Endpoints "http://127.0.0.1:8888", "http://127.0.0.1:8889" `
  -Token "server-specific-token" `
  -KeepWorkspace
```

Run the script separately for servers with different tokens. A failure identifies the HTTP method,
path, status when available, and endpoint without printing credentials or response bodies.

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
