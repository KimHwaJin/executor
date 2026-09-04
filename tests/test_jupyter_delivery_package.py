import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "deploy/jupyter"
HARNESS = REPO / "test_harness/jupyter"
EXTENSION_ROOT = Path("extension/src/executor_resource_extension")


def test_standalone_dockerfile_uses_only_local_copy_sources() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert "test_harness/" not in dockerfile
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        if "--from=" in line:
            continue
        arguments = [
            part for part in line.split()[1:] if not part.startswith("--")
        ]
        for source in arguments[:-1]:
            assert not Path(source).is_absolute()
            assert ".." not in Path(source).parts
            assert (PACKAGE / source).exists()


def test_standalone_package_does_not_reference_test_harness() -> None:
    for path in PACKAGE.rglob("*"):
        if path.is_file():
            assert "test_harness" not in path.read_text(), path


def test_uv_base_images_and_build_only_default_index() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    for root in (PACKAGE, HARNESS):
        image_definition = (root / "Dockerfile").read_text()
        for version in ("3.10", "3.11"):
            argument = "PYTHON" + version.replace(".", "") + "_IMAGE"
            assert (
                f"ARG {argument}=astral/uv:python{version}-bookworm-slim"
                in image_definition
            )
        assert "COPY --from=uv " not in image_definition
        assert "        curl \\" in image_definition
    assert "ARG UV_DEFAULT_INDEX=https://pypi.org/simple" in dockerfile
    assert dockerfile.count("ARG UV_DEFAULT_INDEX") == 3
    assert "UV_NO_CACHE=1" in dockerfile
    assert "UV_LINK_MODE=copy" in dockerfile
    assert not (PACKAGE / "pip.conf").exists()
    assert "/bin/pip install" not in dockerfile


def test_deployment_defaults_are_visible() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    preamble = dockerfile.partition("RUN apt-get update")[0]
    assert (
        "ENV JUPYTER_ROOT_DIR=/workspace/jupyter \\\n    JUPYTER_TOKEN=default"
        in preamble
    )
    assert dockerfile.count("JUPYTER_ROOT_DIR=") == 1
    assert dockerfile.count("JUPYTER_TOKEN=") == 1
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
    assert projects["server"]["project"]["requires-python"] == "==3.11.*"
    assert projects["default"]["project"]["requires-python"] == "==3.11.*"
    assert projects["3102311"]["project"]["requires-python"] == ">=3.10,<3.11"
    assert len(projects["3102311"]["project"]["dependencies"]) == 1
    assert any(
        item.startswith("pandas")
        for item in projects["default"]["project"]["dependencies"]
    )
    assert "FROM ${PYTHON310_IMAGE} AS python310" in dockerfile
    assert "FROM ${PYTHON311_IMAGE}" in dockerfile
    assert "python310-compat" not in dockerfile
    assert "LD_LIBRARY_PATH" not in dockerfile
    assert dockerfile.count("uv sync --project") == 3
    assert dockerfile.count("--locked --no-dev --no-install-project") == 3
    assert dockerfile.count("UV_PROJECT_ENVIRONMENT=/opt/venvs/") == 3
    assert dockerfile.count("uv pip install --strict") == 1
    assert "--system-site-packages" not in dockerfile


def test_delivery_extension_matches_executor_runtime_contract() -> None:
    source_root = HARNESS / EXTENSION_ROOT
    files = list(source_root.rglob("*.py"))
    assert files
    for source in files:
        relative = source.relative_to(HARNESS)
        assert (PACKAGE / relative).read_bytes() == source.read_bytes(), (
            relative
        )
    for relative in [
        "jupyter_server_config.py",
        "executor_resource_extension.json",
        "start-jupyter.sh",
    ]:
        assert (PACKAGE / relative).read_bytes() == (
            HARNESS / relative
        ).read_bytes()
