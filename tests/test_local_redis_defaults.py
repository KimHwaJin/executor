import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("filename", "volume", "old_volume"),
    [
        ("docker-compose.yml", "executor-redis-608", "executor-redis"),
        ("compose.worker-failover.yaml", "redis-data-608", "redis-data"),
    ],
)
def test_compose_pins_redis_and_uses_separate_downgrade_volume(
    filename: str, volume: str, old_volume: str
) -> None:
    compose = yaml.safe_load((ROOT / filename).read_text())
    redis = compose["services"]["redis"]
    assert redis["image"] == "redis:6.0.8"
    assert redis["volumes"] == [f"{volume}:/data"]
    assert volume in compose["volumes"]
    assert old_volume not in compose["volumes"]


@pytest.mark.parametrize("override", [None, "7.4.10"])
def test_integration_gate_checks_expected_redis_version(
    monkeypatch: pytest.MonkeyPatch, override: str | None
) -> None:
    if override is None:
        monkeypatch.delenv("EXECUTOR_EXPECT_REDIS_VERSION", raising=False)
    else:
        monkeypatch.setenv("EXECUTOR_EXPECT_REDIS_VERSION", override)
    script = runpy.run_path(str(ROOT / "scripts" / "quality_gate.py"))
    for check in script["_integration_checks"]():
        assert check.environment["EXECUTOR_EXPECT_REDIS_VERSION"] == (
            override or "6.0.8"
        )
