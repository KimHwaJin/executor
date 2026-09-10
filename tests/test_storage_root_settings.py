"""Storage boundary configuration does not depend on the caller's mount path."""

from pathlib import Path

import pytest

from executor_service.settings import Settings


@pytest.mark.parametrize("input_root", [None, "", "   "])
def test_single_root_is_available_when_input_root_is_unset(input_root):
    settings = Settings(
        _env_file=None,
        shared_storage_root=Path("/mnt/data/executor"),
        input_storage_root=input_root,
    )
    assert (
        settings.effective_input_storage_root == settings.shared_storage_root
    )


def test_environment_separates_input_and_result_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("INPUT_STORAGE_ROOT", str(tmp_path / "agent"))
    monkeypatch.setenv("SHARED_STORAGE_ROOT", str(tmp_path / "executor"))
    settings = Settings(_env_file=None)
    assert settings.effective_input_storage_root == tmp_path / "agent"
    assert settings.shared_storage_root == tmp_path / "executor"
    assert not (tmp_path / "agent").exists()
