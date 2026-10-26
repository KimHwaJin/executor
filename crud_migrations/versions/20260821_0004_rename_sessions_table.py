"""Rename chat_sessions to sessions without losing existing data.

Revision ID: 20260821_0004
Revises: 20260821_0003
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260821_0004"
down_revision: Union[str, None] = "20260821_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 기존 환경만 rename합니다. 수정된 baseline으로 만든 신규 DB에는 이미 sessions가 있습니다.
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.chat_sessions') IS NOT NULL
               AND to_regclass('public.sessions') IS NULL THEN
                ALTER TABLE chat_sessions RENAME TO sessions;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.ix_chat_sessions_project_id') IS NOT NULL THEN
                ALTER INDEX ix_chat_sessions_project_id RENAME TO ix_sessions_project_id;
            END IF;
            IF to_regclass('public.ix_chat_sessions_user_id') IS NOT NULL THEN
                ALTER INDEX ix_chat_sessions_user_id RENAME TO ix_sessions_user_id;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'chat_sessions_project_id_fkey'
                  AND conrelid = 'sessions'::regclass
            ) THEN
                ALTER TABLE sessions RENAME CONSTRAINT
                    chat_sessions_project_id_fkey TO sessions_project_id_fkey;
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'chat_sessions_user_id_fkey'
                  AND conrelid = 'sessions'::regclass
            ) THEN
                ALTER TABLE sessions RENAME CONSTRAINT
                    chat_sessions_user_id_fkey TO sessions_user_id_fkey;
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.sessions') IS NOT NULL
               AND to_regclass('public.chat_sessions') IS NULL THEN
                ALTER TABLE sessions RENAME TO chat_sessions;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.ix_sessions_project_id') IS NOT NULL THEN
                ALTER INDEX ix_sessions_project_id RENAME TO ix_chat_sessions_project_id;
            END IF;
            IF to_regclass('public.ix_sessions_user_id') IS NOT NULL THEN
                ALTER INDEX ix_sessions_user_id RENAME TO ix_chat_sessions_user_id;
            END IF;
        END $$
        """
    )

