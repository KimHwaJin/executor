"""Create reusable Workflow catalog and execution history tables.

Revision ID: ew_0002
Revises: ew_0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "ew_0002"
down_revision = "ew_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_catalog",
        sa.Column("catalog_id", postgresql.UUID(), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_session_id", sa.Text(), nullable=False),
        sa.Column("source_task_id", sa.Text(), nullable=False),
        sa.Column("source_execution_id", postgresql.UUID()),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("workflow_status", sa.Text(), nullable=False),
        sa.Column("workflow", postgresql.JSONB(), nullable=False),
        sa.Column(
            "reusable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "validation_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unverified'"),
        ),
        sa.Column(
            "success_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_execution_status", sa.Text()),
        sa.Column("last_executed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("catalog_id", name="workflow_catalog_pkey"),
        sa.UniqueConstraint(
            "source_task_id",
            "workflow_id",
            "revision",
            name="workflow_catalog_source_revision_key",
        ),
        sa.CheckConstraint(
            "validation_status IN ('unverified', 'validated', 'failed', 'deprecated')",
            name="workflow_catalog_validation_status_check",
        ),
        sa.CheckConstraint(
            "success_count >= 0 AND failure_count >= 0",
            name="workflow_catalog_execution_counts_check",
        ),
    )
    op.create_index(
        "workflow_catalog_recommendation_lookup",
        "workflow_catalog",
        ["intent", "reusable", "validation_status"],
    )

    op.create_table(
        "workflow_executions",
        sa.Column("execution_id", postgresql.UUID(), nullable=False),
        sa.Column("catalog_id", postgresql.UUID()),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("original_workflow", postgresql.JSONB(), nullable=False),
        sa.Column("final_workflow", postgresql.JSONB()),
        sa.Column(
            "execution_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("execution_id", name="workflow_executions_pkey"),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            ["workflow_catalog.catalog_id"],
            name="workflow_executions_catalog_id_fkey",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "workflow_executions_task_lookup",
        "workflow_executions",
        ["session_id", "task_id"],
    )

    op.create_table(
        "workflow_adaptive_history",
        sa.Column("execution_id", postgresql.UUID(), nullable=False),
        sa.Column("decision_round", sa.Integer(), nullable=False),
        sa.Column("changes", postgresql.JSONB(), nullable=False),
        sa.Column("effective_workflow", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "execution_id",
            "decision_round",
            name="workflow_adaptive_history_pkey",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["workflow_executions.execution_id"],
            name="workflow_adaptive_history_execution_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "decision_round > 0",
            name="workflow_adaptive_history_round_check",
        ),
    )

    op.execute(
        """
        CREATE VIEW workflow_adaptive_history_view AS
        SELECT
            h.execution_id,
            h.decision_round,
            e.catalog_id,
            e.workflow_id,
            e.workflow_revision,
            e.session_id,
            e.task_id,
            e.original_workflow,
            h.changes,
            h.effective_workflow,
            e.final_workflow,
            e.execution_status,
            e.started_at,
            h.created_at AS decision_created_at,
            e.finished_at
        FROM workflow_adaptive_history h
        JOIN workflow_executions e
          ON e.execution_id = h.execution_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS workflow_adaptive_history_view")
    op.drop_table("workflow_adaptive_history")
    op.drop_index("workflow_executions_task_lookup", table_name="workflow_executions")
    op.drop_table("workflow_executions")
    op.drop_index(
        "workflow_catalog_recommendation_lookup", table_name="workflow_catalog"
    )
    op.drop_table("workflow_catalog")
