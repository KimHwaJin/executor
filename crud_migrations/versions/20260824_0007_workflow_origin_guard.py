"""Prevent duplicate root workflow candidates per run.

Revision ID: 20260824_0007
Revises: 20260824_0006
Create Date: 2026-08-24
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260824_0007"
down_revision: Union[str, None] = "20260824_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 자동 저장과 HTTP 재시도가 경쟁해도 한 Run당 신규 root candidate는 하나입니다.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_workflows_root_candidate_run
        ON workflows(source_run_id)
        WHERE lifecycle = 'candidate' AND source_workflow_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_workflows_root_candidate_run")

