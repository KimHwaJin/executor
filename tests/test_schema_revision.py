"""Keep application readiness and the Alembic release head aligned."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from executor_service.container import EXPECTED_SCHEMA_REVISION


def test_expected_schema_revision_matches_alembic_head() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    config = Config(str(repository_root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == EXPECTED_SCHEMA_REVISION
