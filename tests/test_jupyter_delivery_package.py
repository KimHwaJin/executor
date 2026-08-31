from configparser import ConfigParser
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
    # The delivery adds its own defaults and package-index configuration.
    shared_build = dockerfile.replace("COPY pip.conf /etc/pip.conf\n", "")
    assert (
        shared_build.partition("RUN apt-get update")[2]
        == (harness_dockerfile.partition("RUN apt-get update")[2])
    )
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        arguments = [
            part for part in line.split()[1:] if not part.startswith("--")
        ]
        for source in arguments[:-1]:
            assert not Path(source).is_absolute()
            assert ".." not in Path(source).parts
            assert (PACKAGE / source).exists()


def test_pip_config_is_installed_before_package_installation() -> None:
    config = ConfigParser()
    config.read(PACKAGE / "pip.conf", encoding="utf-8")
    assert config["global"]["index-url"].endswith("/simple/")
    assert not config.has_option("global", "extra-index-url")
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert dockerfile.index("COPY pip.conf /etc/pip.conf") < dockerfile.index(
        "/bin/pip install"
    )


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
    for kernel, python in [("basic", "3.11"), ("ml", "3.12")]:
        requirements = (
            PACKAGE / "environments" / kernel / "requirements.txt"
        ).read_text()
        packages = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert packages
        assert all(not line.startswith("-") for line in packages)
        assert any(line.startswith("ipykernel") for line in packages)
        assert f"python{python} -m venv /opt/venvs/{kernel}" in dockerfile
        assert f"/opt/venvs/{kernel}/bin/pip install" in dockerfile
        assert f"-r /opt/jupyter-env/{kernel}/requirements.txt" in dockerfile
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
