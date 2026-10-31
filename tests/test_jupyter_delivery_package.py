import re
import tomllib
from pathlib import Path
from string import Template

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "deploy/jupyter"
HARNESS = REPO / "test_harness/jupyter"


def test_standalone_dockerfile_uses_only_local_copy_sources() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert "test_harness/" not in dockerfile
    arguments = dict(re.findall(r"^ARG (\w+)=(.+)$", dockerfile, re.M))
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        if "--from=" in line:
            continue
        copy_args = [
            part for part in line.split()[1:] if not part.startswith("--")
        ]
        for source in copy_args[:-1]:
            source = Template(source).substitute(arguments)
            assert not Path(source).is_absolute()
            assert ".." not in Path(source).parts
            if source.endswith(".tar.xz"):
                # Approved archives are supplied by the deployer, not Git.
                assert Path(source).parent == Path("python-sources")
                assert (PACKAGE / source).parent.is_dir()
                continue
            assert (PACKAGE / source).exists()


def test_standalone_package_does_not_reference_test_harness() -> None:
    for path in PACKAGE.rglob("*"):
        if path.is_file():
            if path.suffixes[-2:] == [".tar", ".xz"]:
                continue
            # Bytecode from running the extension tests; .dockerignore drops it.
            if "__pycache__" in path.parts:
                continue
            assert "test_harness" not in path.read_text(), path


def test_uv_base_images_and_build_only_default_index() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert (
        "ARG PYTHON312_IMAGE=python:3.12-slim-bookworm@sha256:" in dockerfile
    )
    # The official image has no uv: install the pinned version from the
    # build's own index, so a closed network can serve it from Nexus.
    assert (
        'pip install --no-cache-dir --index-url "${UV_DEFAULT_INDEX}" '
        '"uv==${UV_VERSION}"' in dockerfile
    )
    harness_dockerfile = (HARNESS / "Dockerfile").read_text()
    for version in ("3.10", "3.11", "3.12"):
        argument = "PYTHON" + version.replace(".", "") + "_IMAGE"
        assert (
            f"ARG {argument}=python:{version}-slim-bookworm@sha256:"
            in harness_dockerfile
        )
    # Every harness stage installs the same pinned uv as the delivery image.
    assert harness_dockerfile.count('"uv==${UV_VERSION}"') == 3
    assert "ARG UV_VERSION=0.11.32" in harness_dockerfile
    assert "ARG UV_VERSION=0.11.32" in dockerfile
    for image_definition in (dockerfile, harness_dockerfile):
        assert "COPY --from=uv " not in image_definition
        assert "        curl \\" in image_definition
    assert "ARG UV_DEFAULT_INDEX=https://pypi.org/simple" in dockerfile
    assert dockerfile.count("ARG UV_DEFAULT_INDEX") == 2
    assert "UV_NO_CACHE=1" in dockerfile
    assert "UV_LINK_MODE=copy" in dockerfile
    assert not (PACKAGE / "pip.conf").exists()
    assert "/bin/pip install" not in dockerfile


def test_deployment_defaults_are_visible() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    runtime_stage = dockerfile.split("FROM ${PYTHON312_IMAGE}\n")[1]
    assert "ENV JUPYTER_ROOT_DIR=/workspace/jupyter\n" in runtime_stage
    assert dockerfile.count("JUPYTER_ROOT_DIR=") == 1
    # No baked-in token: each server gets its own at runtime.
    assert "JUPYTER_TOKEN=" not in dockerfile
    server_config = (PACKAGE / "jupyter_server_config.py").read_text()
    assert 'os.environ.pop("JUPYTER_TOKEN", None)' in server_config
    assert 'mkdir -p "${JUPYTER_ROOT_DIR}"' in dockerfile
    assert 'chown -R 1000:1000 "${JUPYTER_ROOT_DIR}"' in dockerfile
    assert 'WORKDIR "${JUPYTER_ROOT_DIR}"' in dockerfile


def test_kernel_environments_are_independent() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    projects: dict[str, dict] = {}
    for environment in ("server", "default", "3102311"):
        root = PACKAGE / "environments" / environment
        projects[environment] = tomllib.loads(
            (root / "pyproject.toml").read_text()
        )
        lock = tomllib.loads((root / "uv.lock").read_text())
        assert lock["version"] == 1
        assert lock["package"]
        assert projects[environment]["tool"]["uv"]["package"] is False
        assert not (root / "requirements.txt").exists()

    for kernel in ("default", "3102311"):
        dependencies = projects[kernel]["project"]["dependencies"]
        assert any(item.startswith("ipykernel") for item in dependencies)
    assert projects["server"]["project"]["requires-python"] == "==3.12.*"
    assert projects["default"]["project"]["requires-python"] == "==3.11.*"
    assert projects["3102311"]["project"]["requires-python"] == "==3.10.11"
    assert len(projects["3102311"]["project"]["dependencies"]) == 1
    assert any(
        item.startswith("pandas")
        for item in projects["default"]["project"]["dependencies"]
    )
    assert "FROM ${PYTHON312_IMAGE} AS python-build" in dockerfile
    assert "FROM ${PYTHON312_IMAGE}\n" in dockerfile
    assert "python310-compat" not in dockerfile
    assert "LD_LIBRARY_PATH" not in dockerfile
    assert "COPY environments /opt/jupyter-env" in dockerfile
    assert "RUN sh /tmp/install-environments.sh" in dockerfile
    install = (PACKAGE / "install-environments.sh").read_text()
    # One locked sync per environment, each into its own virtualenv.
    assert 'UV_PROJECT_ENVIRONMENT="$2" uv sync --project "$1"' in install
    assert "--locked --no-dev --no-install-project" in install
    assert 'sync_env "${ENV_ROOT}/server" "${SERVER_VENV}"' in install
    assert '"/opt/venvs/${name}"' in install
    assert "--system-site-packages" not in install


def test_source_build_keeps_versions_and_build_tools_separate() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    builder, runtime = dockerfile.split("FROM ${PYTHON312_IMAGE}\n")
    assert "ARG PYTHON310_VERSION=3.10.11" in builder
    assert "ARG PYTHON311_VERSION=3.11.16" in builder
    kernels = {"3.10": "3102311", "3.11": "default"}
    for minor, kernel in kernels.items():
        assert f"--prefix=/opt/python/{minor}" in builder
        kernel_env = (
            PACKAGE / "environments" / kernel / "kernel.env"
        ).read_text()
        assert (
            f"KERNEL_PYTHON=/opt/python/{minor}/bin/python{minor}"
            in kernel_env
        )
    assert builder.count("make altinstall") == 2
    assert "build-essential" in builder
    assert "build-essential" not in runtime
    assert "COPY --from=python-build /opt/python /opt/python" in runtime
    install = (PACKAGE / "install-environments.sh").read_text()
    assert "/usr/local/bin/python3.12" in install
    assert "UV_PYTHON_DOWNLOADS=never" in runtime
    assert "curl http" not in dockerfile
    assert "uv python install" not in dockerfile
    assert "python-sources/*.tar.xz" in (PACKAGE / ".gitignore").read_text()
    assert "*.tar.xz" not in (PACKAGE / ".dockerignore").read_text()


def test_harness_builds_the_delivered_extension_and_server_files() -> None:
    harness_dockerfile = (HARNESS / "Dockerfile").read_text()
    for relative in [
        "executor_resource_extension.json",
        "jupyter_server_config.py",
        "start-jupyter.sh",
    ]:
        assert f"deploy/jupyter/{relative}" in harness_dockerfile
        assert not (HARNESS / relative).exists()
    assert "COPY deploy/jupyter/extension " in harness_dockerfile
    assert not list((HARNESS / "extension").rglob("src/**/*.py"))
