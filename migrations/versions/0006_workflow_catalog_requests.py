"""Durable request deduplication shared by catalog API and agents."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "ew_0006"
down_revision = "ew_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_catalog_requests",
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("requested_by", "request_key"),
        sa.CheckConstraint("length(request_key) BETWEEN 1 AND 255 AND request_key = btrim(request_key)",
                           name="workflow_catalog_requests_key_check"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="workflow_catalog_requests_hash_check"),
        sa.CheckConstraint("jsonb_typeof(result) = 'object'", name="workflow_catalog_requests_result_check"),
    )


def downgrade():
    op.drop_table("workflow_catalog_requests")
