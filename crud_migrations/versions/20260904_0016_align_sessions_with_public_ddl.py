"""Align sessions columns with public DDL.

Revision ID: 20260904_0016
Revises: 20260903_0015
Create Date: 2026-09-04
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260904_0016"
down_revision: Union[str, None] = "20260903_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS default_model_provider")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS default_model_name")
    op.execute("ALTER TABLE sessions ALTER COLUMN session_name SET DEFAULT '새 대화'")


def downgrade() -> None:
    op.execute("ALTER TABLE sessions ADD COLUMN default_model_provider VARCHAR(50)")
    op.execute("ALTER TABLE sessions ADD COLUMN default_model_name VARCHAR(100)")
    op.execute("ALTER TABLE sessions ALTER COLUMN session_name SET DEFAULT '새 대화'")

