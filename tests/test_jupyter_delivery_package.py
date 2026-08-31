from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "deploy/jupyter"
HARNESS = REPO / "test_harness/jupyter"
EXTENSION_ROOT = Path("extension/src/executor_resource_extension")


def test_standalone_dockerfile_uses_only_local_copy_sources() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert "test_harness/" not in dockerfile
    assert dockerfile == (HARNESS / "Dockerfile").read_text().replace(
        "test_harness/jupyter/", ""
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
