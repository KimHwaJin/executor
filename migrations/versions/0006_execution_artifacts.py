"""Add execution Artifact evidence and lineage.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("execution_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_step_id", sa.Uuid(), nullable=True),
        sa.Column("execution_step_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("parent_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("external_parent_asset_id", sa.String(length=255), nullable=True),
        sa.Column(
            "artifact_type",
            sa.Enum(
                "DATASET",
                "NOTEBOOK",
                "REPORT",
                "PLOT",
                "MODEL",
                "METRIC",
                "LOG",
                "OTHER",
                name="artifact_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "storage_type",
            sa.Enum(
                "PV",
                "S3",
                name="artifact_storage_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "AVAILABLE",
                "INCOMPLETE",
                "DELETED",
                name="artifact_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "artifact_type IN ('DATASET', 'NOTEBOOK', 'REPORT', 'PLOT', 'MODEL', "
            "'METRIC', 'LOG', 'OTHER')",
            name=op.f("ck_execution_artifacts_valid_artifact_type"),
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name=op.f("ck_execution_artifacts_non_negative_size"),
        ),
        sa.CheckConstraint(
            "status IN ('AVAILABLE', 'INCOMPLETE', 'DELETED')",
            name=op.f("ck_execution_artifacts_valid_artifact_status"),
        ),
        sa.CheckConstraint(
            "storage_type IN ('PV', 'S3')",
            name=op.f("ck_execution_artifacts_valid_artifact_storage_type"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_attempt_id"],
            ["execution_attempts.id"],
            name=op.f("fk_execution_artifacts_execution_attempt_id_execution_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_artifacts_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_attempt_id"],
            ["execution_step_attempts.id"],
            name=op.f(
                "fk_execution_artifacts_execution_step_attempt_id_execution_step_attempts"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            name=op.f("fk_execution_artifacts_execution_step_id_execution_steps"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_artifact_id"],
            ["execution_artifacts.id"],
            name=op.f("fk_execution_artifacts_parent_artifact_id_execution_artifacts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_artifacts")),
        sa.UniqueConstraint(
            "identity_hash", name=op.f("uq_execution_artifacts_identity_hash")
        ),
    )
    op.create_index(
        "ix_execution_artifacts_execution_created",
        "execution_artifacts",
        ["execution_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_artifacts_step",
        "execution_artifacts",
        ["execution_step_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_artifacts_step", table_name="execution_artifacts")
    op.drop_index(
        "ix_execution_artifacts_execution_created", table_name="execution_artifacts"
    )
    op.drop_table("execution_artifacts")
