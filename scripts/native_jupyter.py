"""Install, run, and verify the Executor Jupyter runtime without Docker.

The script uses uv-managed Python installations so the same commands work from POSIX shells and
Windows PowerShell. It intentionally uses only the Python standard library itself.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTALL_ROOT = REPOSITORY_ROOT / ".native-jupyter"
DEFAULT_CONTENTS_ROOT = REPOSITORY_ROOT / "notebook_dir"
JUPYTER_REQUIREMENTS = REPOSITORY_ROOT / "docker/jupyter/environments/server/requirements.txt"
BASIC_REQUIREMENTS = REPOSITORY_ROOT / "docker/jupyter/environments/basic/requirements.txt"
ML_REQUIREMENTS = REPOSITORY_ROOT / "docker/jupyter/environments/ml/requirements.txt"
EXTENSION_ROOT = REPOSITORY_ROOT / "docker/jupyter_resource_extension"
SERVER_CONFIG = REPOSITORY_ROOT / "docker/jupyter_server_config.py"
EXTENSION_CONFIG = {"ServerApp": {"jpserver_extensions": {"executor_resource_extension": True}}}


class NativeJupyterError(RuntimeError):
    """A safe, user-actionable native Jupyter setup or verification error."""


def environment_python(environment: Path, *, windows: bool | None = None) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    return environment / ("Scripts/python.exe" if is_windows else "bin/python")


def extension_config_path(server_environment: Path) -> Path:
    # Jupyter's sys.prefix config layout is identical on Windows and POSIX.
    return (
        server_environment / "etc/jupyter/jupyter_server_config.d/executor_resource_extension.json"
    )


def detect_linux_cgroup_root(
    proc_self_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_mount: Path = Path("/sys/fs/cgroup"),
) -> Path | None:
    """Resolve the current process's cgroup v2 leaf instead of assuming the mount root."""
    try:
        lines = proc_self_cgroup.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        hierarchy, controllers, raw_path = line.split(":", maxsplit=2)
        if hierarchy != "0" or controllers:
            continue
        relative = raw_path.lstrip("/")
        candidate = (cgroup_mount / relative).resolve()
        if (candidate / "cpu.stat").is_file():
            return candidate
    return None


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    display = " ".join(_display_argument(argument) for argument in command)
    print(f"+ {display}", flush=True)
    try:
        subprocess.run(command, check=True, env=environment)
    except FileNotFoundError as exc:
        raise NativeJupyterError(f"Command was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise NativeJupyterError(
            f"Command failed with exit code {exc.returncode}: {command[0]}"
        ) from exc


def _display_argument(argument: str) -> str:
    if any(character.isspace() for character in argument):
        return json.dumps(argument)
    return argument


def _required_uv() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise NativeJupyterError(
            "uv is required. Install it first, then ensure the uv command is on PATH."
        )
    return executable


def _environment_roots(install_root: Path) -> dict[str, Path]:
    return {
        "server": install_root / "server",
        "basic": install_root / "basic",
        "ml": install_root / "ml",
    }


def setup(args: argparse.Namespace) -> None:
    uv = _required_uv()
    install_root = Path(args.install_root).expanduser().resolve()
    environments = _environment_roots(install_root)
    install_root.mkdir(parents=True, exist_ok=True)

    _run([uv, "python", "install", "3.11", "3.12"])
    for name, version in (("server", "3.12"), ("basic", "3.11"), ("ml", "3.12")):
        _run(
            [
                uv,
                "venv",
                "--no-project",
                "--clear",
                "--seed",
                "--python",
                version,
                str(environments[name]),
            ]
        )

    server_python = environment_python(environments["server"])
    basic_python = environment_python(environments["basic"])
    ml_python = environment_python(environments["ml"])
    for python, requirements in (
        (server_python, JUPYTER_REQUIREMENTS),
        (basic_python, BASIC_REQUIREMENTS),
        (ml_python, ML_REQUIREMENTS),
    ):
        _run(
            [
                uv,
                "pip",
                "install",
                "--strict",
                "--python",
                str(python),
                "--requirements",
                str(requirements),
            ]
        )

    _run(
        [
            uv,
            "pip",
            "install",
            "--strict",
            "--python",
            str(server_python),
            "--no-deps",
            str(EXTENSION_ROOT),
        ]
    )
    _run(
        [
            str(basic_python),
            "-m",
            "ipykernel",
            "install",
            "--prefix",
            str(environments["server"]),
            "--name",
            "basic",
            "--display-name=Basic (Python 3.11)",
        ]
    )
    _run(
        [
            str(ml_python),
            "-m",
            "ipykernel",
            "install",
            "--prefix",
            str(environments["server"]),
            "--name",
            "ml",
            "--display-name=ML (Python 3.12)",
        ]
    )

    default_kernelspec = environments["server"] / "share/jupyter/kernels/python3"
    if default_kernelspec.exists():
        shutil.rmtree(default_kernelspec)
    config_path = extension_config_path(environments["server"])
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(EXTENSION_CONFIG, indent=2) + "\n", encoding="utf-8")
    _verify_local_install(environments)
    print(f"Native Jupyter environments are ready under: {install_root}")


def _verify_local_install(environments: dict[str, Path]) -> None:
    expected = {"server": (3, 12), "basic": (3, 11), "ml": (3, 12)}
    for name, version in expected.items():
        python = environment_python(environments[name])
        if not python.is_file():
            raise NativeJupyterError(f"{name} Python was not created: {python}")
        result = subprocess.run(
            [
                str(python),
                "-c",
                "import json,sys; "
                "print(json.dumps([sys.version_info.major,sys.version_info.minor]))",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        actual = tuple(json.loads(result.stdout))
        if actual != version:
            raise NativeJupyterError(f"{name} requires Python {version}, found {actual}.")

    server_python = environment_python(environments["server"])
    _run(
        [
            str(server_python),
            "-c",
            "import executor_resource_extension, jupyterlab; print('server packages: ready')",
        ]
    )
    kernels = environments["server"] / "share/jupyter/kernels"
    advertised = {path.name for path in kernels.iterdir() if path.is_dir()}
    if advertised != {"basic", "ml"}:
        raise NativeJupyterError(
            f"Expected exactly basic and ml kernelspecs, found: {sorted(advertised)}"
        )


def run_server(args: argparse.Namespace, extra_arguments: list[str]) -> None:
    install_root = Path(args.install_root).expanduser().resolve()
    environments = _environment_roots(install_root)
    server_python = environment_python(environments["server"])
    if not server_python.is_file():
        raise NativeJupyterError("Native Jupyter is not installed. Run the setup command first.")

    token = args.token or os.getenv("JUPYTER_TOKEN", "")
    if not token:
        raise NativeJupyterError(
            "JUPYTER_TOKEN is required. Set it in the shell or pass --token for local testing."
        )
    root_dir = Path(args.root_dir).expanduser().resolve()
    root_dir.mkdir(parents=True, exist_ok=True)

    environment = os.environ.copy()
    environment["JUPYTER_TOKEN"] = token
    environment["JUPYTER_ROOT_DIR"] = str(root_dir)
    if args.cpu_cores is not None:
        environment["EXECUTOR_RESOURCE_CPU_CORES"] = str(args.cpu_cores)
    if args.memory_bytes is not None:
        environment["EXECUTOR_RESOURCE_MEMORY_BYTES"] = str(args.memory_bytes)
    if "EXECUTOR_RESOURCE_CGROUP_ROOT" not in environment and sys.platform.startswith("linux"):
        detected = detect_linux_cgroup_root()
        if detected is not None:
            environment["EXECUTOR_RESOURCE_CGROUP_ROOT"] = str(detected)

    command = [
        str(server_python),
        "-m",
        "jupyterlab",
        "--config",
        str(SERVER_CONFIG),
        f"--ServerApp.ip={args.host}",
        f"--ServerApp.port={args.port}",
        "--ServerApp.open_browser=False",
        "--ServerApp.answer_yes=True",
        f"--ServerApp.root_dir={root_dir}",
    ]
    if args.base_url != "/":
        command.append(f"--ServerApp.base_url={args.base_url}")
    command.extend(extra_arguments)
    _run(command, environment=environment)


def verify(args: argparse.Namespace) -> None:
    endpoint = args.endpoint.rstrip("/")
    token = args.token or os.getenv("JUPYTER_TOKEN", "")
    if not token:
        raise NativeJupyterError("JUPYTER_TOKEN or --token is required for verification.")

    status = _json_request(endpoint, token, "GET", "/api/status")
    kernelspecs = _json_request(endpoint, token, "GET", "/api/kernelspecs")
    resources = _json_request(endpoint, token, "GET", "/executor/resource-status")
    advertised = set(kernelspecs.get("kernelspecs", {}))
    if advertised != {"basic", "ml"}:
        raise NativeJupyterError(
            f"Expected exactly basic and ml kernelspecs, found: {sorted(advertised)}"
        )
    if resources.get("schema_version") != "1.0":
        raise NativeJupyterError("Executor resource endpoint returned an unsupported schema.")

    started_kernels: list[str] = []
    try:
        for profile in ("basic", "ml"):
            kernel = _json_request(
                endpoint,
                token,
                "POST",
                "/api/kernels",
                {"name": profile},
            )
            kernel_id = kernel.get("id")
            if not isinstance(kernel_id, str) or not kernel_id:
                raise NativeJupyterError(f"Jupyter did not return a kernel ID for {profile}.")
            started_kernels.append(kernel_id)
            _ensure_kernel_available(endpoint, token, kernel_id, profile)
    finally:
        for kernel_id in started_kernels:
            _request_no_content(endpoint, token, "DELETE", f"/api/kernels/{kernel_id}")

    workspace = f"native-verification/{uuid4()}"
    prepared = _json_request(
        endpoint,
        token,
        "POST",
        "/executor/storage/workspaces/prepare",
        {"workspace_path": workspace},
    )
    snapshot = _json_request(
        endpoint,
        token,
        "POST",
        "/executor/storage/artifacts/snapshot",
        {"workspace_path": workspace},
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "active_kernels": status.get("kernels"),
                "kernelspecs": sorted(advertised),
                "kernel_lifecycle": "verified",
                "resource_observation": resources,
                "workspace": prepared,
                "artifact_snapshot": snapshot,
            },
            indent=2,
        )
    )


def _json_request(
    endpoint: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{endpoint}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except HTTPError as exc:
        raise NativeJupyterError(f"Jupyter returned HTTP {exc.code} for {path}.") from exc
    except URLError as exc:
        raise NativeJupyterError(f"Jupyter is unreachable at {endpoint}.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeJupyterError(f"Jupyter returned invalid JSON for {path}.") from exc
    if not isinstance(result, dict):
        raise NativeJupyterError(f"Jupyter returned a non-object response for {path}.")
    return result


def _request_no_content(endpoint: str, token: str, method: str, path: str) -> None:
    request = Request(
        f"{endpoint}{path}",
        method=method,
        headers={"Authorization": f"token {token}"},
    )
    try:
        with urlopen(request, timeout=30):
            return
    except HTTPError as exc:
        raise NativeJupyterError(f"Jupyter returned HTTP {exc.code} for {path}.") from exc
    except URLError as exc:
        raise NativeJupyterError(f"Jupyter is unreachable at {endpoint}.") from exc


def _ensure_kernel_available(
    endpoint: str,
    token: str,
    kernel_id: str,
    profile: str,
    *,
    observation_seconds: float = 2,
) -> None:
    """Ensure the kernel remains registered while its process starts.

    A kernel started only through Jupyter's REST API can remain in ``starting`` until a client
    opens its channels, so waiting for ``idle`` here would reject a healthy, unused kernel.
    """
    deadline = time.monotonic() + observation_seconds
    while time.monotonic() < deadline:
        kernel = _json_request(endpoint, token, "GET", f"/api/kernels/{kernel_id}")
        if kernel.get("id") != kernel_id:
            raise NativeJupyterError(f"Jupyter returned an invalid kernel record for {profile}.")
        time.sleep(0.25)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.set_defaults(action=None)
    subcommands = root.add_subparsers(dest="action", required=True)

    setup_parser = subcommands.add_parser("setup", help="Install server/basic/ml environments.")
    setup_parser.add_argument("--install-root", default=str(DEFAULT_INSTALL_ROOT))

    run_parser = subcommands.add_parser("run", help="Run the native JupyterLab server.")
    run_parser.add_argument("--install-root", default=str(DEFAULT_INSTALL_ROOT))
    run_parser.add_argument("--root-dir", default=str(DEFAULT_CONTENTS_ROOT))
    run_parser.add_argument("--host", default="127.0.0.1")
    run_parser.add_argument("--port", type=int, default=8888)
    run_parser.add_argument("--base-url", default="/")
    run_parser.add_argument("--token", help="Prefer the JUPYTER_TOKEN environment variable.")
    run_parser.add_argument("--cpu-cores", type=float)
    run_parser.add_argument("--memory-bytes", type=int)

    verify_parser = subcommands.add_parser("verify", help="Verify a running Jupyter target.")
    verify_parser.add_argument("--endpoint", default="http://127.0.0.1:8888")
    verify_parser.add_argument("--token", help="Prefer the JUPYTER_TOKEN environment variable.")
    return root


def main() -> None:
    arguments, extra = parser().parse_known_args()
    if arguments.action == "run" and extra[:1] == ["--"]:
        extra = extra[1:]
    try:
        if arguments.action == "setup":
            if extra:
                raise NativeJupyterError(f"Unexpected setup arguments: {extra}")
            setup(arguments)
        elif arguments.action == "run":
            run_server(arguments, extra)
        elif arguments.action == "verify":
            if extra:
                raise NativeJupyterError(f"Unexpected verify arguments: {extra}")
            verify(arguments)
    except NativeJupyterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
