"""Cross-platform path and cgroup helpers for native Jupyter bootstrap."""

from argparse import Namespace
from pathlib import Path

import pytest

from test_harness.jupyter import native
from test_harness.jupyter.native import (
    NativeJupyterError,
    detect_linux_cgroup_root,
    environment_python,
)


def test_environment_python_uses_platform_specific_virtualenv_layout() -> None:
    root = Path("runtime")
    assert environment_python(root, windows=False) == root / "bin/python"
    assert environment_python(root, windows=True) == root / "Scripts/python.exe"


def test_detects_current_cgroup_v2_leaf(tmp_path: Path) -> None:
    mount = tmp_path / "cgroup"
    leaf = mount / "user.slice/session.scope"
    leaf.mkdir(parents=True)
    (leaf / "cpu.stat").write_text("usage_usec 1\n", encoding="utf-8")
    proc = tmp_path / "proc-self-cgroup"
    proc.write_text("0::/user.slice/session.scope\n", encoding="utf-8")

    assert detect_linux_cgroup_root(proc, mount) == leaf


def test_missing_or_v1_cgroup_returns_none(tmp_path: Path) -> None:
    proc = tmp_path / "proc-self-cgroup"
    proc.write_text("2:cpu:/legacy\n", encoding="utf-8")

    assert detect_linux_cgroup_root(proc, tmp_path / "cgroup") is None


def test_setup_uses_explicit_pythons_and_nexus_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python_311 = tmp_path / "python311.exe"
    python_312 = tmp_path / "python312.exe"
    python_311.touch()
    python_312.touch()
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    monkeypatch.setattr(native, "_required_uv", lambda: "uv")
    monkeypatch.setattr(
        native,
        "_run",
        lambda command, *, environment=None: calls.append((command, environment)),
    )
    monkeypatch.setattr(native, "_verify_local_install", lambda environments: None)

    native.setup(
        Namespace(
            install_root=str(tmp_path / "install"),
            python_311=str(python_311),
            python_312=str(python_312),
            index_url="https://nexus.example/repository/pypi-group/simple",
        )
    )

    assert not any(command[:3] == ["uv", "python", "install"] for command, _ in calls)
    venv_commands = [command for command, _ in calls if command[:2] == ["uv", "venv"]]
    assert [command[command.index("--python") + 1] for command in venv_commands] == [
        str(python_312.resolve()),
        str(python_311.resolve()),
        str(python_312.resolve()),
    ]
    uv_environments = [environment for command, environment in calls if command[0] == "uv"]
    assert uv_environments
    assert all(
        environment is not None
        and environment["UV_DEFAULT_INDEX"] == "https://nexus.example/repository/pypi-group/simple"
        for environment in uv_environments
    )


def test_setup_downloads_only_python_without_explicit_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python_311 = tmp_path / "python311.exe"
    python_311.touch()
    commands: list[list[str]] = []

    monkeypatch.setattr(native, "_required_uv", lambda: "uv")
    monkeypatch.setattr(
        native,
        "_run",
        lambda command, *, environment=None: commands.append(command),
    )
    monkeypatch.setattr(native, "_verify_local_install", lambda environments: None)

    native.setup(
        Namespace(
            install_root=str(tmp_path / "install"),
            python_311=str(python_311),
            python_312=None,
            index_url=None,
        )
    )

    assert ["uv", "python", "install", "3.12"] in commands


def test_setup_rejects_missing_explicit_python(tmp_path: Path) -> None:
    with pytest.raises(NativeJupyterError, match=r"Python 3\.11 executable does not exist"):
        native._python_selector(str(tmp_path / "missing-python.exe"), "3.11")
