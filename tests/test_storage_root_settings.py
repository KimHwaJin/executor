"""Storage root configuration does not depend on the caller's mount path."""

from executor_service.settings import Settings


def test_environment_sets_the_shared_storage_root(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "shared"))
    settings = Settings(_env_file=None)
    assert settings.storage_root == tmp_path / "shared"
    assert not (tmp_path / "shared").exists()
