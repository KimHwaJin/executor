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


def test_execution_service_support_does_not_import_public_facade() -> None:
    support_package = SOURCE_ROOT / "application" / "_execution_service"
    violations = {
        path.name: sorted(
            name
            for name in _imports(path)
            if name == "executor_service.application.services"
        )
        for path in _python_files(support_package)
    }
    assert not {path: names for path, names in violations.items() if names}


def test_execution_service_delegates_command_responsibilities() -> None:
    imports = _imports(SOURCE_ROOT / "application" / "services.py")
    delegated = {
        "executor_service.domain.errors",
        "executor_service.tracing",
        "executor_service.work_messages",
        "hashlib",
        "json",
    }
    assert imports.isdisjoint(delegated)


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


def test_worker_facade_does_not_own_persistence_queries() -> None:
    imports = _imports(
        SOURCE_ROOT / "infrastructure" / "execution_worker" / "worker.py"
    )
    assert "sqlalchemy" not in imports
    assert "executor_service.infrastructure.db.models" not in imports


def test_execution_query_facade_does_not_own_persistence_queries() -> None:
    imports = _imports(SOURCE_ROOT / "infrastructure" / "execution_queries.py")
    assert "executor_service.infrastructure.db.models" not in imports
    assert not any(
        name == "sqlalchemy" or name.startswith("sqlalchemy.sql")
        for name in imports
    )


def test_result_storage_support_does_not_import_public_facade() -> None:
    support_package = SOURCE_ROOT / "infrastructure" / "_result_storage"
    violations = {
        path.name: sorted(
            name
            for name in _imports(path)
            if name == "executor_service.infrastructure.result_storage"
        )
        for path in _python_files(support_package)
    }
    assert not {path: names for path, names in violations.items() if names}


def test_result_storage_facade_delegates_file_format() -> None:
    imports = _imports(SOURCE_ROOT / "infrastructure" / "result_storage.py")
    delegated = {
        "base64",
        "binascii",
        "hashlib",
        "json",
        "os",
        "re",
        "shutil",
    }
    assert imports.isdisjoint(delegated)


def test_execution_query_readers_do_not_import_public_facade() -> None:
    query_package = SOURCE_ROOT / "infrastructure" / "_execution_queries"
    violations = {
        path.name: sorted(
            name
            for name in _imports(path)
            if name == "executor_service.infrastructure.execution_queries"
        )
        for path in _python_files(query_package)
    }
    assert not {path: names for path, names in violations.items() if names}


def test_runtime_registry_support_does_not_import_public_facade() -> None:
    support_package = SOURCE_ROOT / "infrastructure" / "_runtime_registry"
    violations = {
        path.name: sorted(
            name
            for name in _imports(path)
            if name == "executor_service.infrastructure.runtime_registry"
        )
        for path in _python_files(support_package)
    }
    assert not {path: names for path, names in violations.items() if names}


def test_runtime_registry_delegates_support_responsibilities() -> None:
    imports = _imports(SOURCE_ROOT / "infrastructure" / "runtime_registry.py")
    delegated = {
        "asyncio",
        "cryptography.fernet",
        "executor_service.infrastructure.runtime_admission",
        "executor_service.infrastructure.runtime_drivers",
        "hashlib",
        "json",
        "logging",
    }
    assert imports.isdisjoint(delegated)
    assert "executor_service.infrastructure.db.models" not in imports
    assert not any(
        name == "sqlalchemy" or name.startswith("sqlalchemy.sql")
        for name in imports
    )


def test_internal_contract_modules_do_not_import_public_facade() -> None:
    contract_package = SOURCE_ROOT / "interfaces" / "_contracts"
    violations = {
        path.name: sorted(
            name
            for name in _imports(path)
            if name == "executor_service.interfaces.contracts"
        )
        for path in _python_files(contract_package)
    }
    assert not {path: names for path, names in violations.items() if names}


def test_public_contract_facade_only_reexports_internal_contracts() -> None:
    facade = SOURCE_ROOT / "interfaces" / "contracts.py"
    tree = ast.parse(facade.read_text(encoding="utf-8"), filename=str(facade))
    definitions = [
        node.name
        for node in tree.body
        if isinstance(
            node,
            (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        )
    ]
    assert definitions == []
    assert all(
        name.startswith("executor_service.interfaces._contracts")
        for name in _imports(facade)
    )
