"""Add the agent role to chat messages.

Revision ID: 20260903_0013
Revises: 20260831_0012
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260903_0013"
down_revision: Union[str, None] = "20260831_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL enum은 Agent 중간 로그와 최종 assistant 답변을 DB에서도 구분한다.
    op.execute("ALTER TYPE message_type ADD VALUE IF NOT EXISTS 'agent'")


def downgrade() -> None:
    # PostgreSQL enum value 제거에는 타입 재생성이 필요하고 기존 데이터 손실 위험이 있어 유지한다.
    pass

