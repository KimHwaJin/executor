"""Remove messages.parent_message_id.

Revision ID: 20260821_0003
Revises: 20260821_0002
Create Date: 2026-08-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260821_0003"
down_revision: Union[str, None] = "20260821_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("fk_messages_parent_same_session", "messages", type_="foreignkey")
    op.drop_constraint("ck_messages_not_self_parent", "messages", type_="check")
    op.drop_index("ix_messages_parent_message_id", table_name="messages")
    op.drop_column("messages", "parent_message_id")


def downgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("parent_message_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_messages_parent_message_id", "messages", ["parent_message_id"])
    op.create_check_constraint(
        "ck_messages_not_self_parent",
        "messages",
        "parent_message_id IS NULL OR parent_message_id <> message_id",
    )
    op.create_foreign_key(
        "fk_messages_parent_same_session",
        "messages",
        "messages",
        ["parent_message_id", "session_id"],
        ["message_id", "session_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

