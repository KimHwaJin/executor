"""Add E13 global workflow catalog and normalized tags.

Revision ID: 20260824_0006
Revises: 20260824_0005
Create Date: 2026-08-24
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260824_0006"
down_revision: Union[str, None] = "20260824_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Workflow는 user/project/session에 귀속시키지 않습니다. created_by는 권한/감사용입니다.
    op.execute(
        """
        CREATE TABLE workflows (
            workflow_id UUID PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            lifecycle VARCHAR(20) NOT NULL DEFAULT 'candidate',
            file_path VARCHAR(1000) NOT NULL UNIQUE,
            content_sha256 VARCHAR(64) NOT NULL,
            source_run_id UUID NOT NULL REFERENCES agent_runs(run_id) ON DELETE RESTRICT,
            source_workflow_id UUID REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
            created_by_user_id UUID REFERENCES users(user_id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT ck_workflows_lifecycle CHECK (lifecycle IN ('candidate', 'template'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE workflow_tags (
            workflow_tag_id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
            tag VARCHAR(50) NOT NULL,
            CONSTRAINT uq_workflow_tags_workflow_tag UNIQUE (workflow_id, tag)
        )
        """
    )
    op.execute("CREATE INDEX ix_workflows_lifecycle ON workflows(lifecycle)")
    op.execute("CREATE INDEX ix_workflows_source_run_id ON workflows(source_run_id)")
    op.execute("CREATE INDEX ix_workflows_source_workflow_id ON workflows(source_workflow_id)")
    op.execute("CREATE INDEX ix_workflows_created_by_user_id ON workflows(created_by_user_id)")
    op.execute(
        "CREATE INDEX ix_workflows_active_created ON workflows(lifecycle, created_at) WHERE deleted_at IS NULL"
    )
    op.execute("CREATE INDEX ix_workflow_tags_workflow_id ON workflow_tags(workflow_id)")
    op.execute("CREATE INDEX ix_workflow_tags_tag ON workflow_tags(tag)")


def downgrade() -> None:
    op.execute("DROP TABLE workflow_tags")
    op.execute("DROP TABLE workflows")

