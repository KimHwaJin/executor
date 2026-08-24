"""Add normalized Runtime Output Journal metadata.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _audit_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("created_by_type", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by_type", sa.String(length=32), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _audit_constraints() -> tuple[sa.CheckConstraint, ...]:
    return (
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name="valid_created_by_type",
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name="valid_updated_by_type",
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name="complete_created_by",
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name="complete_updated_by",
        ),
    )


def upgrade() -> None:
    op.create_table(
        "execution_output_journals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("execution_step_id", sa.Uuid(), nullable=False),
        sa.Column("execution_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_step_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_target_id", sa.Uuid(), nullable=False),
        sa.Column(
            "runtime_session_id", sa.String(length=1024), nullable=False
        ),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("committed_offset", sa.BigInteger(), nullable=False),
        sa.Column("output_count", sa.BigInteger(), nullable=False),
        sa.Column("representation_count", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("abort_reason", sa.Text(), nullable=True),
        *_audit_columns(),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_constraints(),
        sa.CheckConstraint("sequence >= 0", name="non_negative_sequence"),
        sa.CheckConstraint("fencing_token > 0", name="positive_fencing_token"),
        sa.CheckConstraint(
            "state IN ('OPEN', 'FINALIZED', 'ABORTED')",
            name="valid_state",
        ),
        sa.CheckConstraint(
            "committed_offset >= 0", name="non_negative_committed_offset"
        ),
        sa.CheckConstraint(
            "output_count >= 0", name="non_negative_output_count"
        ),
        sa.CheckConstraint(
            "representation_count >= 0",
            name="non_negative_representation_count",
        ),
        sa.CheckConstraint(
            "total_bytes >= 0", name="non_negative_total_bytes"
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["execution_operations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_attempt_id"],
            ["execution_attempts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_attempt_id"],
            ["execution_step_attempts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["runtime_target_id"], ["runtime_targets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_attempt_id",
            "execution_step_id",
            "fencing_token",
            name="uq_output_journals_attempt_step_fence",
        ),
    )
    op.create_index(
        op.f("ix_execution_output_journals_execution_id"),
        "execution_output_journals",
        ["execution_id"],
    )
    op.create_index(
        "ix_output_journals_execution_sequence",
        "execution_output_journals",
        ["execution_id", "sequence"],
    )

    op.create_table(
        "execution_outputs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("execution_step_id", sa.Uuid(), nullable=False),
        sa.Column("execution_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("stream_name", sa.String(length=32), nullable=True),
        sa.Column("execution_count", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_audit_columns(),
        *_audit_constraints(),
        sa.CheckConstraint("sequence >= 0", name="non_negative_sequence"),
        sa.CheckConstraint("ordinal >= 0", name="non_negative_ordinal"),
        sa.CheckConstraint(
            "kind IN ('STREAM', 'DISPLAY', 'RESULT', 'ERROR')",
            name="valid_kind",
        ),
        sa.CheckConstraint(
            "execution_count IS NULL OR execution_count >= 0",
            name="non_negative_execution_count",
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"],
            ["execution_output_journals.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["execution_operations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_attempt_id"],
            ["execution_attempts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "journal_id", "ordinal", name="uq_outputs_journal_ordinal"
        ),
    )
    op.create_index(
        op.f("ix_execution_outputs_execution_id"),
        "execution_outputs",
        ["execution_id"],
    )
    op.create_index(
        op.f("ix_execution_outputs_journal_id"),
        "execution_outputs",
        ["journal_id"],
    )
    op.create_index(
        "ix_outputs_execution_created_cursor",
        "execution_outputs",
        ["execution_id", "created_at", "id"],
    )
    op.create_index(
        "ix_outputs_step_ordinal",
        "execution_outputs",
        ["execution_step_id", "ordinal"],
    )

    op.create_table(
        "execution_output_representations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("output_id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("content_ref", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_audit_columns(),
        *_audit_constraints(),
        sa.CheckConstraint("size_bytes >= 0", name="non_negative_size_bytes"),
        sa.ForeignKeyConstraint(
            ["output_id"],
            ["execution_outputs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_ref", name="uq_output_representations_ref"
        ),
    )
    op.create_index(
        op.f("ix_execution_output_representations_output_id"),
        "execution_output_representations",
        ["output_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_execution_output_representations_output_id"),
        table_name="execution_output_representations",
    )
    op.drop_table("execution_output_representations")
    op.drop_index("ix_outputs_step_ordinal", table_name="execution_outputs")
    op.drop_index(
        "ix_outputs_execution_created_cursor",
        table_name="execution_outputs",
    )
    op.drop_index(
        op.f("ix_execution_outputs_journal_id"),
        table_name="execution_outputs",
    )
    op.drop_index(
        op.f("ix_execution_outputs_execution_id"),
        table_name="execution_outputs",
    )
    op.drop_table("execution_outputs")
    op.drop_index(
        "ix_output_journals_execution_sequence",
        table_name="execution_output_journals",
    )
    op.drop_index(
        op.f("ix_execution_output_journals_execution_id"),
        table_name="execution_output_journals",
    )
    op.drop_table("execution_output_journals")
