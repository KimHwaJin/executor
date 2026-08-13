"""Cross-platform path and cgroup helpers for native Jupyter bootstrap."""

from pathlib import Path

from scripts.native_jupyter import detect_linux_cgroup_root, environment_python


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
