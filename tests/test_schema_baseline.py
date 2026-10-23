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
    assert len(revisions) == 8
    assert revisions[0].revision == EXPECTED_SCHEMA_REVISION == "0008"
    expected = ["0008", "0007", "0006", "0005", "0004", "0003", "0002", "0001"]
    assert [revision.revision for revision in revisions] == expected
    assert [revision.down_revision for revision in revisions] == [
        *expected[1:],
        None,
    ]
