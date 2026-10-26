"""Backfill graph-generated agent message roles.

Revision ID: 20260903_0014
Revises: 20260903_0013
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260903_0014"
down_revision: Union[str, None] = "20260903_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Graph 중간 결과는 최종 사용자 답변과 구분한다. 원본 graph_name이 있어
    # 과거 레코드도 안전하게 식별 가능하며 FAQ/파일조회/리포트는 assistant로 유지한다.
    op.execute(
        """
        UPDATE messages
           SET message_type = 'agent'
         WHERE message_type = 'assistant'
           AND metadata->>'graph_name' IS NOT NULL
           AND metadata->>'graph_name' NOT IN ('faq', 'file_lookup', 'report_writer')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE messages
           SET message_type = 'assistant'
         WHERE message_type = 'agent'
        """
    )

