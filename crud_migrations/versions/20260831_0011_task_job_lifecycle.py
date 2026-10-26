"""Rebuild Task as a long-lived analysis job with many AgentRuns.

Revision ID: 20260831_0011
Revises: 20260826_0010
Create Date: 2026-08-31
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260831_0011"
down_revision: Union[str, None] = "20260826_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE task_status AS ENUM "
        "('pending','running','waiting_input','success','error','timeout','canceled')"
    )
    op.execute("DROP INDEX uq_tasks_session_active")
    op.execute("ALTER TABLE tasks ADD COLUMN root_run_id UUID")
    op.execute("ALTER TABLE tasks ADD COLUMN checkpoint_run_id UUID")
    op.execute("ALTER TABLE agent_runs ADD COLUMN task_id UUID")

    # 기존 1:1 관계를 보존한 채 FK 방향을 AgentRun → Task로 뒤집습니다.
    op.execute("UPDATE tasks SET root_run_id = run_id, checkpoint_run_id = run_id")
    op.execute(
        "UPDATE agent_runs ar SET task_id = t.task_id FROM tasks t WHERE t.run_id = ar.run_id"
    )
    op.execute("ALTER TABLE tasks ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE tasks ALTER COLUMN status TYPE task_status "
        "USING (CASE WHEN status::text = 'interrupted' THEN 'waiting_input' ELSE status::text END)::task_status"
    )
    op.execute("ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'running'::task_status")

    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT fk_tasks_root_run "
        "FOREIGN KEY(root_run_id) REFERENCES agent_runs(run_id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT fk_tasks_checkpoint_run "
        "FOREIGN KEY(checkpoint_run_id) REFERENCES agent_runs(run_id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT fk_agent_runs_task "
        "FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE SET NULL"
    )
    op.execute("CREATE INDEX ix_tasks_root_run_id ON tasks(root_run_id)")
    op.execute("CREATE INDEX ix_tasks_checkpoint_run_id ON tasks(checkpoint_run_id)")
    op.execute("CREATE INDEX ix_agent_runs_task_id ON agent_runs(task_id)")

    op.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_run_id_key")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_run_id_fkey")
    op.execute("ALTER TABLE tasks DROP COLUMN run_id")
    op.execute(
        "CREATE UNIQUE INDEX uq_tasks_session_active ON tasks(session_id) "
        "WHERE status IN ('pending','running')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_tasks_session_active")
    op.execute("ALTER TABLE tasks ADD COLUMN run_id UUID")
    op.execute("UPDATE tasks SET run_id = root_run_id")
    op.execute("DELETE FROM tasks WHERE run_id IS NULL")
    op.execute("ALTER TABLE tasks ALTER COLUMN run_id SET NOT NULL")
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT tasks_run_id_fkey "
        "FOREIGN KEY(run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE tasks ADD CONSTRAINT tasks_run_id_key UNIQUE(run_id)")
    op.execute("ALTER TABLE tasks ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE tasks ALTER COLUMN status TYPE agent_run_status "
        "USING (CASE WHEN status::text = 'waiting_input' THEN 'interrupted' ELSE status::text END)::agent_run_status"
    )
    op.execute("ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'running'::agent_run_status")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT fk_agent_runs_task")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT fk_tasks_checkpoint_run")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT fk_tasks_root_run")
    op.execute("DROP INDEX ix_agent_runs_task_id")
    op.execute("DROP INDEX ix_tasks_checkpoint_run_id")
    op.execute("DROP INDEX ix_tasks_root_run_id")
    op.execute("ALTER TABLE agent_runs DROP COLUMN task_id")
    op.execute("ALTER TABLE tasks DROP COLUMN checkpoint_run_id")
    op.execute("ALTER TABLE tasks DROP COLUMN root_run_id")
    op.execute(
        "CREATE UNIQUE INDEX uq_tasks_session_active ON tasks(session_id) "
        "WHERE status IN ('pending','running')"
    )
    op.execute("DROP TYPE task_status")

