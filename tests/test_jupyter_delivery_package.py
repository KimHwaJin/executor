from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "deploy/jupyter"
HARNESS = REPO / "test_harness/jupyter"
EXTENSION_ROOT = Path("extension/src/executor_resource_extension")


def test_standalone_dockerfile_uses_only_local_copy_sources() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert "test_harness/" not in dockerfile
    harness_dockerfile = (
        (HARNESS / "Dockerfile")
        .read_text()
        .replace("test_harness/jupyter/", "")
        .replace("/workspace/pv", '"${JUPYTER_ROOT_DIR}"')
    )
    assert (
        dockerfile.partition("RUN apt-get update")[2]
        == (harness_dockerfile.partition("RUN apt-get update")[2])
    )
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


def test_uv_is_pinned_and_uses_a_build_only_default_index() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert "ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.8" in dockerfile
    assert (
        "ARG UV_DEFAULT_INDEX="
        "https://nexus.example.com/repository/pypi-group/simple/"
        in dockerfile
    )
    assert dockerfile.count("COPY --from=uv /uv /uvx /bin/") == 2
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
    for kernel in ("default", "3102311"):
        requirements = (
            PACKAGE / "environments" / kernel / "requirements.txt"
        ).read_text()
        packages = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert all(not line.startswith("-") for line in packages)
        assert not any(line.startswith("ipykernel") for line in packages)
        assert f"--python /opt/venvs/{kernel}/bin/python" in dockerfile
    default_requirements = (
        PACKAGE / "environments/default/requirements.txt"
    ).read_text()
    assert default_requirements.strip()
    requirements_3102311 = (
        PACKAGE / "environments/3102311/requirements.txt"
    ).read_text()
    assert requirements_3102311 == ""
    assert "FROM python:3.10.11-slim-bullseye AS python310" in dockerfile
    assert "FROM python:3.11-slim-bullseye" in dockerfile
    assert "python310-compat" not in dockerfile
    assert "LD_LIBRARY_PATH" not in dockerfile
    assert dockerfile.count("uv venv --no-project --clear") == 3
    assert dockerfile.count("uv pip install --strict") == 6
    assert dockerfile.count('"ipykernel>=6.30,<7"') == 2
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
