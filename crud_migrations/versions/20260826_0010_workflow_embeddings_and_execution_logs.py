"""Add workflow search embeddings and execution history.

Revision ID: 20260826_0010
Revises: 20260824_0009
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260826_0010"
down_revision: Union[str, None] = "20260824_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 초기 배포 template은 Agent Run 없이 등록될 수 있으므로 source_run_id를 선택값으로 바꿉니다.
    op.execute("ALTER TABLE workflows ALTER COLUMN source_run_id DROP NOT NULL")
    op.execute("ALTER TABLE workflows ADD COLUMN goal TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE workflows ADD COLUMN schema_version VARCHAR(30) NOT NULL DEFAULT '1.0'")
    op.execute("ALTER TABLE workflows ADD COLUMN is_recommendable BOOLEAN NOT NULL DEFAULT false")
    # 기존 승격 template은 현재 서비스에서 이미 공개된 자산이므로 추천 허용 상태로 이관합니다.
    op.execute("UPDATE workflows SET is_recommendable = true WHERE lifecycle = 'template' AND deleted_at IS NULL")

    op.execute(
        """
        CREATE TABLE workflow_embeddings (
            embedding_id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
            embedded_text TEXT NOT NULL,
            embedded_text_sha256 VARCHAR(64) NOT NULL,
            search_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            model_provider VARCHAR(100) NOT NULL,
            model_name VARCHAR(200) NOT NULL,
            model_revision VARCHAR(100) NOT NULL DEFAULT 'default',
            dimensions INTEGER NOT NULL,
            vector_values DOUBLE PRECISION[],
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            failure_reason TEXT,
            is_active BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            embedded_at TIMESTAMPTZ,
            CONSTRAINT ck_workflow_embeddings_dimensions CHECK (dimensions > 0),
            CONSTRAINT ck_workflow_embeddings_status CHECK (status IN ('pending', 'ready', 'failed', 'superseded')),
            CONSTRAINT ck_workflow_embeddings_ready_vector CHECK (
                status <> 'ready' OR (vector_values IS NOT NULL AND cardinality(vector_values) = dimensions)
            ),
            CONSTRAINT uq_workflow_embeddings_source_model UNIQUE (
                workflow_id, model_provider, model_name, model_revision, embedded_text_sha256
            )
        )
        """
    )
    op.execute("CREATE INDEX ix_workflow_embeddings_workflow_id ON workflow_embeddings(workflow_id)")
    op.execute("CREATE INDEX ix_workflow_embeddings_search_metadata ON workflow_embeddings USING gin(search_metadata)")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_workflow_embeddings_active_model
        ON workflow_embeddings(workflow_id, model_provider, model_name, model_revision)
        WHERE is_active = true AND status = 'ready'
        """
    )

    op.execute(
        """
        CREATE TABLE workflow_execution_logs (
            workflow_log_id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
            agent_run_id UUID REFERENCES agent_runs(run_id) ON DELETE SET NULL,
            task_id UUID REFERENCES tasks(task_id) ON DELETE SET NULL,
            execution_id UUID,
            message_id UUID REFERENCES messages(message_id) ON DELETE SET NULL,
            workflow_content_sha256 VARCHAR(64) NOT NULL,
            attempt_no INTEGER NOT NULL DEFAULT 1,
            status VARCHAR(20) NOT NULL,
            input_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            result JSONB NOT NULL DEFAULT '{}'::jsonb,
            error_code VARCHAR(100),
            error_message TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            duration_ms INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_workflow_execution_logs_attempt CHECK (attempt_no > 0),
            CONSTRAINT ck_workflow_execution_logs_duration CHECK (duration_ms IS NULL OR duration_ms >= 0),
            CONSTRAINT ck_workflow_execution_logs_status CHECK (
                status IN ('queued', 'running', 'interrupted', 'success', 'failed', 'canceled', 'timeout')
            ),
            CONSTRAINT uq_workflow_execution_logs_attempt UNIQUE(workflow_id, agent_run_id, attempt_no)
        )
        """
    )
    for column in ("workflow_id", "agent_run_id", "task_id", "execution_id", "message_id"):
        op.execute(f"CREATE INDEX ix_workflow_execution_logs_{column} ON workflow_execution_logs({column})")


def downgrade() -> None:
    op.execute("DROP TABLE workflow_execution_logs")
    op.execute("DROP TABLE workflow_embeddings")
    op.execute("ALTER TABLE workflows DROP COLUMN is_recommendable")
    op.execute("ALTER TABLE workflows DROP COLUMN schema_version")
    op.execute("ALTER TABLE workflows DROP COLUMN goal")
    # 초기 template의 NULL source_run_id가 있으면 되돌릴 수 없으므로 명시적으로 실패시키는 것이 안전합니다.
    op.execute("ALTER TABLE workflows ALTER COLUMN source_run_id SET NOT NULL")

