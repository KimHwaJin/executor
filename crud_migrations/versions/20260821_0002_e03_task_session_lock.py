"""Add E03 tasks and agent run logs.

Revision ID: 20260821_0002
Revises: 20260821_0001
Create Date: 2026-08-21
"""
from typing import Sequence, Union

from alembic import op


revision: str = "20260821_0002"
down_revision: Union[str, None] = "20260821_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # AgentRun은 모든 Graph 실행 기록, Task는 분석 작업/Session 잠금으로 분리합니다.
    op.execute("ALTER TABLE agent_runs ADD CONSTRAINT uq_agent_runs_session_idempotency UNIQUE (session_id, idempotency_key)")
    op.execute("""CREATE TABLE tasks (
        task_id UUID PRIMARY KEY,
        run_id UUID NOT NULL UNIQUE REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        trigger_message_id UUID REFERENCES messages(message_id) ON DELETE RESTRICT,
        trigger_type VARCHAR(30) NOT NULL DEFAULT 'pending_route',
        status agent_run_status NOT NULL DEFAULT 'running',
        idempotency_key VARCHAR(255) NOT NULL,
        lock_owner VARCHAR(255), lock_token UUID,
        heartbeat_at TIMESTAMPTZ, lease_expires_at TIMESTAMPTZ,
        failure_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ,
        CONSTRAINT uq_tasks_session_idempotency UNIQUE (session_id, idempotency_key)
    )""")
    op.execute("CREATE INDEX ix_tasks_session_id ON tasks (session_id)")
    op.execute("CREATE INDEX ix_tasks_lease_expires_at ON tasks (lease_expires_at)")
    op.execute("CREATE UNIQUE INDEX uq_tasks_session_active ON tasks (session_id) WHERE status IN ('pending', 'running')")
    op.execute("""CREATE TABLE agent_run_logs (
        log_id UUID PRIMARY KEY,
        run_id UUID NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
        event_key VARCHAR(500) NOT NULL,
        agent_name VARCHAR(100), node VARCHAR(100) NOT NULL,
        event VARCHAR(100) NOT NULL, kind VARCHAR(50) NOT NULL,
        payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_agent_run_logs_event UNIQUE (run_id, event_key)
    )""")
    op.execute("CREATE INDEX ix_agent_run_logs_run_id ON agent_run_logs (run_id)")
    op.execute("CREATE INDEX ix_agent_run_logs_agent_name ON agent_run_logs (agent_name)")


def downgrade() -> None:
    op.execute("DROP TABLE agent_run_logs")
    op.execute("DROP TABLE tasks")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT uq_agent_runs_session_idempotency")

