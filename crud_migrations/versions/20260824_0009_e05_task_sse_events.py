"""Add durable task SSE events.

Revision ID: 20260824_0009
Revises: 20260824_0008
Create Date: 2026-08-24
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260824_0009"
down_revision: Union[str, None] = "20260824_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN last_event_sequence BIGINT NOT NULL DEFAULT 0")
    op.execute(
        """
        CREATE TABLE task_events (
            task_event_id UUID PRIMARY KEY,
            task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            run_id UUID NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
            sequence BIGINT NOT NULL,
            event_type VARCHAR(100) NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_task_events_task_sequence UNIQUE(task_id, sequence)
        )
        """
    )
    op.execute("CREATE INDEX ix_task_events_run_id ON task_events(run_id)")
    op.execute("CREATE INDEX ix_task_events_event_type ON task_events(event_type)")
    op.execute("CREATE INDEX ix_task_events_task_sequence ON task_events(task_id, sequence)")


def downgrade() -> None:
    op.execute("DROP TABLE task_events")
    op.execute("ALTER TABLE tasks DROP COLUMN last_event_sequence")

