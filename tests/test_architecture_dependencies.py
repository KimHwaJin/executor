"""Dependency-direction checks for the Executor source tree."""

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "executor_service"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def _python_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_domain_does_not_depend_on_outer_layers() -> None:
    forbidden = (
        "executor_service.application",
        "executor_service.infrastructure",
        "executor_service.interfaces",
    )
    violations = {
        str(path.relative_to(SOURCE_ROOT)): sorted(
            name for name in _imports(path) if name.startswith(forbidden)
        )
        for path in _python_files(SOURCE_ROOT / "domain")
    }
    assert not {path: names for path, names in violations.items() if names}


def test_application_does_not_depend_on_adapters() -> None:
    forbidden = (
        "executor_service.infrastructure",
        "executor_service.interfaces",
    )
    violations = {
        str(path.relative_to(SOURCE_ROOT)): sorted(
            name for name in _imports(path) if name.startswith(forbidden)
        )
        for path in _python_files(SOURCE_ROOT / "application")
    }
    assert not {path: names for path, names in violations.items() if names}


def test_worker_collaborators_do_not_import_facade() -> None:
    worker_package = SOURCE_ROOT / "infrastructure" / "execution_worker"
    violations = {
        path.name: sorted(
            name
            for name in _imports(path)
            if name
            == "executor_service.infrastructure.execution_worker.worker"
        )
        for path in _python_files(worker_package)
        if path.name not in {"__init__.py", "worker.py"}
    }
    assert not {path: names for path, names in violations.items() if names}
