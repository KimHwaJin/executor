"""Separate execution plans from reusable versions and retire legacy tables.

Revision ID: ew_0004
Revises: ew_0003
"""
from alembic import context, op
import sqlalchemy as sa

revision = "ew_0004"
down_revision = "ew_0003"
branch_labels = None
depends_on = None


VIEW = """
CREATE VIEW workflow_adaptive_history_view AS
SELECT h.execution_id, h.decision_round, e.execution_plan_id, e.version_id,
       e.workflow_id, e.workflow_revision, e.session_id, e.task_id,
       e.original_workflow, e.execution_inputs, h.changes, h.effective_workflow,
       e.final_workflow, e.execution_status, e.successful, e.started_at,
       h.created_at AS decision_created_at, e.finished_at
FROM workflow_adaptive_history h JOIN workflow_executions e USING (execution_id)
"""


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("Execution history migration requires online preservation checks")
    op.execute("LOCK TABLE workflow_catalog_legacy, workflow_catalog_legacy_mapping, workflow_executions IN ACCESS EXCLUSIVE MODE")
    # Copy every row, including unselected candidates and old validation counters.
    # Keep IDs unchanged so durable checkpoints can still identify their plans.
    op.execute("""
        CREATE TABLE workflow_execution_plans (
            execution_plan_id UUID PRIMARY KEY,
            version_id UUID REFERENCES workflow_version(version_id) ON DELETE RESTRICT,
            workflow_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            intent TEXT NOT NULL,
            compiled_workflow JSONB NOT NULL,
            legacy_metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (task_id, workflow_id, revision),
            UNIQUE (execution_plan_id, version_id),
            CHECK (jsonb_typeof(compiled_workflow) = 'object')
        )
    """)
    op.execute("""
        INSERT INTO workflow_execution_plans
        (execution_plan_id, version_id, workflow_id, revision, session_id, task_id,
         intent, compiled_workflow, legacy_metadata, created_at, updated_at)
        SELECT c.catalog_id, m.version_id, c.workflow_id, c.revision,
               c.source_session_id, c.source_task_id, c.intent, c.workflow,
               to_jsonb(c), c.created_at, c.updated_at
        FROM workflow_catalog_legacy c
        LEFT JOIN workflow_catalog_legacy_mapping m USING (catalog_id)
    """)
    op.execute("DROP VIEW workflow_adaptive_history_view")
    op.drop_constraint("workflow_executions_catalog_id_fkey", "workflow_executions", type_="foreignkey")
    op.alter_column("workflow_executions", "catalog_id", new_column_name="execution_plan_id")
    op.execute("ALTER TABLE workflow_executions ADD COLUMN successful BOOLEAN")
    op.execute("ALTER TABLE workflow_executions ADD COLUMN version_id UUID REFERENCES workflow_version(version_id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE workflow_executions ADD COLUMN execution_inputs JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(execution_inputs) = 'array')")
    op.execute("""
        UPDATE workflow_executions e SET version_id=p.version_id
        FROM workflow_execution_plans p WHERE e.execution_plan_id=p.execution_plan_id
    """)
    # Historical 1.3 inputs remain intact in original_workflow. Preserve an
    # explicit execution_inputs array when present; do not infer absent values.
    op.execute("""
        UPDATE workflow_executions SET execution_inputs=original_workflow->'workflow'->'execution_inputs'
        WHERE jsonb_typeof(original_workflow->'workflow'->'execution_inputs')='array'
    """)
    op.execute("""
        ALTER TABLE workflow_executions ADD CONSTRAINT workflow_executions_plan_fkey
        FOREIGN KEY (execution_plan_id) REFERENCES workflow_execution_plans(execution_plan_id) ON DELETE RESTRICT
    """)
    op.execute("""
        ALTER TABLE workflow_executions ADD CONSTRAINT workflow_executions_plan_version_fkey
        FOREIGN KEY (execution_plan_id,version_id)
        REFERENCES workflow_execution_plans(execution_plan_id,version_id) ON DELETE RESTRICT
    """)
    op.execute("CREATE INDEX workflow_executions_version_lookup ON workflow_executions(version_id)")
    op.execute("""
        CREATE FUNCTION workflow_check_execution_version() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_id UUID; expected_revision INTEGER; actual_revision INTEGER;
        BEGIN
            IF NEW.version_id IS NULL THEN RETURN NEW; END IF;
            SELECT workflow_id,revision INTO expected_id,expected_revision
                FROM workflow_version WHERE version_id=NEW.version_id;
            actual_revision := COALESCE((to_jsonb(NEW)->>'revision')::integer,
                                        (to_jsonb(NEW)->>'workflow_revision')::integer);
            IF expected_id IS NULL OR NEW.workflow_id<>expected_id::text OR actual_revision<>expected_revision THEN
                RAISE EXCEPTION 'Execution workflow ID/revision does not match version' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER workflow_execution_plan_version_check
        BEFORE INSERT OR UPDATE OF version_id,workflow_id,revision ON workflow_execution_plans
        FOR EACH ROW EXECUTE FUNCTION workflow_check_execution_version()
    """)
    op.execute("""
        CREATE TRIGGER workflow_execution_version_check
        BEFORE INSERT OR UPDATE OF version_id,workflow_id,workflow_revision ON workflow_executions
        FOR EACH ROW EXECUTE FUNCTION workflow_check_execution_version()
    """)
    if not context.is_offline_mode():
        bind = op.get_bind()
        lost = bind.execute(sa.text("""
            SELECT EXISTS (
                SELECT 1 FROM workflow_catalog_legacy c LEFT JOIN workflow_execution_plans p
                ON p.execution_plan_id=c.catalog_id
                WHERE p.execution_plan_id IS NULL OR p.legacy_metadata IS DISTINCT FROM to_jsonb(c)
                   OR p.compiled_workflow IS DISTINCT FROM c.workflow
            )
        """)).scalar()
        if lost:
            raise RuntimeError("Legacy execution plan preservation verification failed")
        mismatch = bind.execute(sa.text("""
            SELECT EXISTS (
                SELECT 1 FROM workflow_execution_plans p JOIN workflow_version v USING (version_id)
                WHERE p.workflow_id<>v.workflow_id::text OR p.revision<>v.revision
                UNION ALL
                SELECT 1 FROM workflow_executions e JOIN workflow_version v USING(version_id)
                WHERE e.workflow_id<>v.workflow_id::text OR e.workflow_revision<>v.revision
            )
        """)).scalar()
        if mismatch:
            raise RuntimeError("Legacy version mapping does not match workflow ID/revision; migration rolled back")
    else:
        raise RuntimeError("Execution history migration requires online preservation checks")
    # Restrict (no CASCADE): any unreviewed external dependency blocks the entire
    # transaction rather than silently removing foreign keys or application data.
    op.drop_table("workflow_catalog_legacy_mapping")
    op.drop_table("workflow_catalog_legacy")
    op.execute(VIEW)


def downgrade():
    # New execution plans cannot safely be converted back to the old validation
    # and source-execution metadata. Never silently drop post-migration history.
    raise RuntimeError("ew_0004 downgrade requires a reviewed restoration migration; execution history is preserved")
