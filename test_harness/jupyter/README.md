# Executor Jupyter Test Harness

This directory owns the JupyterLab image used by Executor Runtime Targets. Image build,
kernel-package maintenance, authentication, workspace mounting, and the resource observation
extension are documented here rather than in the Executor service README.

## Image layout

The final image is built from a pinned uv and Python 3.11 Bookworm Slim image and runs JupyterLab
as the non-root `jovyan` user (UID/GID 1000). Python 3.10.x is copied from a matching uv and Python
3.10 Bookworm Slim build stage. Both stages use the same Debian generation.
Three isolated virtual environments are created:

| Environment | Python | Purpose | Package list |
| --- | --- | --- | --- |
| Jupyter server | 3.11 | JupyterLab and server process | `environments/server/requirements.txt` |
| `default` kernel | 3.11 | General data analysis | `environments/default/requirements.txt` |
| `3102311` kernel | 3.10.x | Project-specific environment | `environments/3102311/requirements.txt` |

Only the `default` and `3102311` kernelspecs are exposed. The default kernel is `default`.
`3102311` remains the stable kernelspec ID; the actual Python patch version comes from the selected
`PYTHON310_IMAGE` and may be any supported Python 3.10.x release.
The two environments are independent. `3102311/requirements.txt` is intentionally empty so the
approved package list can be pasted directly. Kernel bootstrap dependency `ipykernel` is installed
by the Docker/native setup and must not be added to either user package list. Both Docker and native
setup use `uv venv` and `uv pip install`; `requirements.txt` remains the package-list input format.

The harness mirrors the deployment image's Debian 12 Bookworm base and kernel layout.

## Build

Run the build from the repository root because the Dockerfile uses the repository as its build
context:

```bash
docker build --tag executor-jupyter:local --file test_harness/jupyter/Dockerfile .
```

The Dockerfile uses pinned Python 3.10 and 3.11 uv images. In a closed network, import both images
into the internal registry and point package resolution at Nexus:

```bash
docker build \
  --build-arg PYTHON310_IMAGE=harbor.example.com/library/uv:python3.10-bookworm-slim \
  --build-arg PYTHON311_IMAGE=harbor.example.com/library/uv:python3.11-bookworm-slim \
  --build-arg UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
  --tag executor-jupyter:local \
  --file test_harness/jupyter/Dockerfile .
```

uv does not read `pip.conf`; Docker builds use the `UV_DEFAULT_INDEX` build argument. Never place
repository credentials in the Dockerfile or build argument. The example assumes anonymous access
inside the trusted network.

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

The authenticated extension prepares workspaces, creates the initial notebook, snapshots
Runtime-produced artifacts, computes file metadata and SHA-256 on the Jupyter side, and reads
append-only artifact manifests. Notebook reads use Jupyter's standard Contents API;
Executor-managed notebook writes use the extension's `prepare` and `project` endpoints below.
File scans and hashing run in a worker thread so they do not block Jupyter's server event loop;
Executor applies `JUPYTER_STORAGE_TIMEOUT_SECONDS` (default 300) to these potentially slower calls.

The extension exposes Executor-only notebook preparation and file-content operations:

- `POST /executor/storage/notebooks/prepare`
- `POST /executor/storage/notebooks/project`
- `GET /executor/storage/files/content?path=...` (optional HTTP `Range` header)

`prepare` atomically creates stable Executor-managed code cells before their Operation runs;
`project` atomically replaces that notebook with the latest sealed shared-volume results. Neither
operation uses NbModelClient, YDoc, RTC, or Jupyter checkpoints, so an already open JupyterLab
document may require a reload. Complete Step outputs are not stored separately by this extension.
Agents never receive the Jupyter token and do not call extension-internal endpoints.
The file-content operation streams the current whole file (`200`) when Range is omitted, or an
inclusive single byte range (`206`). Invalid ranges return `416` and the current file size.
Executor uses it for registered PV Artifact downloads; it rejects absolute paths, traversal, and
non-files. This route is internal and is not a replacement for the public Artifact API.

The handler opens the file **before** obtaining its size (`fstat`). Size, SHA-256, range validation
and body reads use that same descriptor, held until download completion/disconnect. SHA-256 is
calculated by a full-file bounded scan before headers, including for Range requests; plan storage
I/O and setup timeouts accordingly. No additional persistent metadata/download copy is written.
Atomic replacement of the path keeps POSIX readers on the originally opened file. In-place writes
are only detected on a best-effort basis; this is not a filesystem snapshot or a lock on manual
Jupyter/analysis-tool saves. Windows open-file replacement behavior depends on filesystem/sharing
rules and is not given the POSIX guarantee. Download after saving when using those writers.

Deploy the updated Executor **and** rebuilt Jupyter extension/image together. The old `start`/`end`
query bounds are removed; old clients are rejected rather than silently receiving a full file.
An old extension lacking current-file download metadata is also rejected by the new Executor.

### Notebook permissions and nbviewer

On POSIX systems, Executor-managed notebooks are written with mode `0644`: the owner can read
and write, and other users can read. This lets a separate nbviewer process, including
UID 65534 (`nobody`), read the notebook on a shared PV. Notebook execution does not require an
executable file bit. Only grant access to this PV to services allowed to read its notebook code
and outputs; preferably mount it read-only in nbviewer.

The extension sets `0644` on the temporary file before the atomic replacement, both for initial
creation and subsequent projection. A previous `0600` notebook becomes `0644` when rewritten.
If setting permissions or saving fails, the existing notebook is not replaced. This policy does
not change directory permissions, ownership, other artifacts, or shared result files. All parent
directories must also allow nbviewer's user to traverse them (`x` permission). On Windows,
access is still controlled by NTFS ACLs; the POSIX permission operation is skipped.

To deploy this change, rebuild and redeploy the **Jupyter image**, or reinstall the updated
extension in the actual Jupyter server environment and restart Jupyter. Updating only the
Executor service image does not update the Jupyter extension.

Existing files are not scanned or changed at startup. For an already-generated `0600` notebook,
an operator can run the following as its owner inside the Jupyter container, replacing the
example with the exact absolute path of the affected file:

```bash
stat -c '%a %u:%g %n' '/actual/jupyter/root/users/u/projects/p/sessions/s/executions/e/notebooks/execution.ipynb'
chmod 0644 '/actual/jupyter/root/users/u/projects/p/sessions/s/executions/e/notebooks/execution.ipynb'
stat -c '%a %u:%g %n' '/actual/jupyter/root/users/u/projects/p/sessions/s/executions/e/notebooks/execution.ipynb'
```

Do not recursively change permissions on the whole PV. A one-time `chmod` alone is insufficient
if the old extension remains deployed: its next atomic rewrite can recreate the file as `0600`.

Regression tests cover new files, replacement of existing files, restrictive umasks, preservation
of unrelated permissions, and failures during publication. The opt-in Linux/root test
`extension/tests/test_notebook_shared_read.py` additionally checks a UID 1000:GID 100 writer
and a UID 65534:GID 65534 reader, including nbformat validation and nbconvert HTML rendering.
Run this identity-switching test only in a disposable container; it is skipped by default on
non-Linux or non-root hosts and is not an image build step.

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

The kernelspec response must advertise `default` and `3102311` only. Runtime Target registration and
scheduling remain Executor service concerns and are documented in the repository root README.

## Native installation without Docker

The repository also provides one Python entry point for Linux, macOS, and Windows PowerShell. It
uses uv-managed CPython, creates three isolated environments under `test_harness/jupyter/.native`, installs the
same requirements and Executor extension as the image, and exposes only the `default` and `3102311`
kernels. WSL is not required on Windows.

Prerequisites:

- `uv` available on `PATH`;
- network access to the configured Python package index during setup;
- 64-bit Windows when using binary analysis packages;
- Microsoft Visual C++ Redistributable on Windows when required by pasted packages;
- package-specific system libraries required by the approved requirements;
- `libgomp` on Linux, normally provided by `libgomp1` or the equivalent distribution package.

The setup command is the same in POSIX shells and PowerShell. From the repository root:

```text
uv run python test_harness/jupyter/native.py setup
```

The command installs uv-managed Python 3.11 and exact Python 3.10.11 when they are absent. It does not alter the
system Python installation. Re-run it after changing a requirements file or the Jupyter extension. Existing
`.native/basic` and `.native/ml` directories from the previous layout are ignored and reported; verify the new
`.native/default` and `.native/3102311` environments before removing those legacy directories manually.

### Windows installation through an internal Nexus

The default setup command can download managed Python distributions and packages from public
repositories. For a closed Windows network, distribute `uv.exe` and the official 64-bit Python
3.11 and exact 3.10.11 installers through an approved internal channel, install both Python
versions, and publish all packages required by `environments/server`, `environments/default`,
`environments/3102311`, plus `ipykernel>=6.30,<7`, to a Nexus PyPI repository.

Resolve the installed Python executables and run the bootstrap script directly with Python 3.11.
Supplying both executable paths prevents `native.py` from running `uv python install`, while
`--index-url` replaces the public PyPI default for every package installation:

```powershell
$Python311 = py -3.11 -c "import sys; print(sys.executable)"
$Python310 = py -3.10 -c "import sys; assert sys.version_info[:3] == (3, 10, 11); print(sys.executable)"
$NexusIndex = "https://nexus.example/repository/pypi-group/simple"

# Use the Windows certificate store when Nexus is signed by an internal corporate CA.
$env:UV_SYSTEM_CERTS = "true"

& $Python311 test_harness\jupyter\native.py setup `
  --python-311 $Python311 `
  --python-310 $Python310 `
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

The diagnostic defaults to the `default` kernel. Run it once more with `-Profile 3102311` to
verify the exact Python 3.10.11 kernel on the same Windows server:

```powershell
.\test_harness\jupyter\scripts\windows_rest_diagnostics.ps1 `
  -Endpoints "http://127.0.0.1:8888" `
  -Token "server-specific-token" `
  -Profile 3102311
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
