"""Store execution snapshots directly; retire the intermediate plan table.

Revision ID: ew_0005
Revises: ew_0004
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from alembic import context, op
import sqlalchemy as sa

revision = "ew_0005"
down_revision = "ew_0004"
branch_labels = None
depends_on = None


VIEW = """
CREATE VIEW workflow_adaptive_history_view AS
SELECT h.execution_id, h.decision_round, e.version_id,
       e.workflow_id, e.workflow_revision, e.session_id, e.task_id,
       e.original_workflow, e.execution_inputs, h.changes, h.effective_workflow,
       e.final_workflow, e.execution_status, e.successful, e.started_at,
       h.created_at AS decision_created_at, e.finished_at
FROM workflow_adaptive_history h JOIN workflow_executions e USING (execution_id)
"""


def archive_plans(bind):
    records = [dict(row) for row in bind.execute(sa.text(
        "SELECT * FROM workflow_execution_plans ORDER BY execution_plan_id"
    )).mappings()]
    if not records:
        return
    # Unexecuted candidates must not be fabricated into execution records or
    # reusable 1.0 documents. Preserve the old rows in a one-time export only.
    root = Path(__file__).resolve().parents[2]
    directory = Path(context.get_x_argument(as_dictionary=True).get(
        "execution_plans_archive_dir", str(root / "var" / "workflow_migration_archives")
    )).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"execution_plans_ew_0005_{uuid4().hex}.json"
    payload = json.dumps({"source_revision": "ew_0004", "target_revision": "ew_0005",
                          "archived_at": datetime.now(timezone.utc).isoformat(),
                          "record_count": len(records), "records": records},
                         ensure_ascii=False, default=str, indent=2).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(payload).digest():
        raise RuntimeError("Execution plan export verification failed; no plans removed")
    print(f"Preserved {len(records)} historical plans in {path}")


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("Execution snapshot migration requires online preservation checks")
    op.execute("LOCK TABLE workflow_execution_plans, workflow_executions IN ACCESS EXCLUSIVE MODE")
    # Historical candidate export disabled at the user's request. Execution
    # snapshots and version references remain in workflow_executions.
    # archive_plans(op.get_bind())
    op.execute("DROP VIEW workflow_adaptive_history_view")
    op.drop_constraint("workflow_executions_plan_version_fkey", "workflow_executions", type_="foreignkey")
    op.drop_constraint("workflow_executions_plan_fkey", "workflow_executions", type_="foreignkey")
    op.drop_column("workflow_executions", "execution_plan_id")
    op.drop_table("workflow_execution_plans")  # Never CASCADE external dependencies.
    op.execute("""
        CREATE OR REPLACE FUNCTION workflow_check_execution_version() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_id UUID; expected_revision INTEGER;
        BEGIN
            IF NEW.version_id IS NULL THEN RETURN NEW; END IF;
            SELECT workflow_id,revision INTO expected_id,expected_revision
                FROM workflow_version WHERE version_id=NEW.version_id;
            IF expected_id IS NULL OR NEW.workflow_id<>expected_id::text OR NEW.workflow_revision<>expected_revision THEN
                RAISE EXCEPTION 'Execution workflow ID/revision does not match version' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(VIEW)


def downgrade():
    raise RuntimeError("ew_0005 downgrade requires a reviewed restoration migration")
