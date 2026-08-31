"""The checked-in migration head must match application readiness."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from executor_service.container import EXPECTED_SCHEMA_REVISION


def test_current_schema_keeps_baseline_and_additive_upgrades() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    revisions = list(scripts.walk_revisions())

    assert scripts.get_heads() == [EXPECTED_SCHEMA_REVISION]
    assert len(revisions) == 3
    assert revisions[0].revision == EXPECTED_SCHEMA_REVISION == "0003"
    assert revisions[0].down_revision == "0002"
    assert revisions[1].revision == "0002"
    assert revisions[1].down_revision == "0001"
    assert revisions[2].revision == "0001"
    assert revisions[2].down_revision is None
