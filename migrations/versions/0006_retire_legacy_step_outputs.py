"""Switch Step results to bounded summaries and journal references.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25

The legacy output body columns are intentionally retained as write-disabled
data so upgrading cannot destroy results created before Output Journals were
available. A later, explicitly destructive migration may drop them after the
operator confirms that historical data no longer needs to be retained.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_OUTPUT_SUMMARY = sa.text(
    "json_build_object("
    "'output_count', 0, 'output_types', json_build_object(), "
    "'stream_names', json_build_array(), "
    "'mime_types', json_build_array(), 'has_image', false, "
    "'image_count', 0, 'has_error', false)"
)
_EMPTY_OUTPUTS = sa.text("'[]'::json")


def upgrade() -> None:
    op.add_column(
        "execution_steps",
        sa.Column(
            "output_summary",
            sa.JSON(),
            nullable=False,
            server_default=_EMPTY_OUTPUT_SUMMARY,
        ),
    )
    op.add_column(
        "execution_steps",
        sa.Column("result_execution_attempt_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_execution_steps_result_execution_attempt_id",
        "execution_steps",
        "execution_attempts",
        ["result_execution_attempt_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "execution_step_attempts",
        sa.Column(
            "output_summary",
            sa.JSON(),
            nullable=False,
            server_default=_EMPTY_OUTPUT_SUMMARY,
        ),
    )

    # New application versions no longer map or write these columns. The
    # defaults keep INSERTs valid while preserving all pre-upgrade values.
    op.alter_column(
        "execution_steps", "outputs", server_default=_EMPTY_OUTPUTS
    )
    op.alter_column(
        "execution_step_attempts", "outputs", server_default=_EMPTY_OUTPUTS
    )
    op.alter_column("execution_steps", "output_summary", server_default=None)
    op.alter_column(
        "execution_step_attempts", "output_summary", server_default=None
    )


def downgrade() -> None:
    op.alter_column("execution_steps", "outputs", server_default=None)
    op.alter_column("execution_step_attempts", "outputs", server_default=None)
    op.drop_column("execution_step_attempts", "output_summary")
    op.drop_constraint(
        "fk_execution_steps_result_execution_attempt_id",
        "execution_steps",
        type_="foreignkey",
    )
    op.drop_column("execution_steps", "result_execution_attempt_id")
    op.drop_column("execution_steps", "output_summary")
