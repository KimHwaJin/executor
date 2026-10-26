"""Add E10-T04 Jupyter server registry.

Revision ID: 20260824_0008
Revises: 20260824_0007
Create Date: 2026-08-24
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260824_0008"
down_revision: Union[str, None] = "20260824_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jupyter_servers (
            jupyter_server_id UUID PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            endpoint VARCHAR(1000) NOT NULL,
            token_ciphertext TEXT,
            health_status VARCHAR(20) NOT NULL DEFAULT 'unknown',
            last_http_status INTEGER,
            last_latency_ms INTEGER,
            last_error VARCHAR(500),
            last_checked_at TIMESTAMPTZ,
            created_by_user_id UUID REFERENCES users(user_id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT ck_jupyter_servers_health_status
                CHECK (health_status IN ('unknown', 'healthy', 'unhealthy'))
        )
        """
    )
    op.execute("CREATE INDEX ix_jupyter_servers_health_status ON jupyter_servers(health_status)")
    op.execute("CREATE INDEX ix_jupyter_servers_last_checked_at ON jupyter_servers(last_checked_at)")
    op.execute("CREATE INDEX ix_jupyter_servers_created_by_user_id ON jupyter_servers(created_by_user_id)")
    op.execute("CREATE UNIQUE INDEX uq_jupyter_servers_active_name ON jupyter_servers(name) WHERE deleted_at IS NULL")
    op.execute("CREATE UNIQUE INDEX uq_jupyter_servers_active_endpoint ON jupyter_servers(endpoint) WHERE deleted_at IS NULL")


def downgrade() -> None:
    op.execute("DROP TABLE jupyter_servers")

