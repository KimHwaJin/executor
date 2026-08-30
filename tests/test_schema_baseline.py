"""The checked-in migration head must match application readiness."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from executor_service.container import EXPECTED_SCHEMA_REVISION


def test_current_schema_is_a_single_initial_revision() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    revisions = list(scripts.walk_revisions())

    assert scripts.get_heads() == [EXPECTED_SCHEMA_REVISION]
    assert len(revisions) == 1
    assert revisions[0].revision == EXPECTED_SCHEMA_REVISION == "0001"
    assert revisions[0].down_revision is None
