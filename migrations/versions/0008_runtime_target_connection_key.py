"""Allow one Runtime Target registration per normalized endpoint.

Revision ID: 0008
Revises: 0007
"""

from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _normalize(endpoint: str) -> str:
    # Frozen copy of normalize_endpoint as of this revision.
    parts = urlsplit(endpoint.strip())
    scheme = parts.scheme
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    return f"{scheme}://{host}{parts.path.rstrip('/')}"


def upgrade() -> None:
    # Runtime Target registration is a rare operator action. A 0007 replica
    # cannot insert a new Target until the rollout finishes.
    op.add_column(
        "runtime_targets",
        sa.Column("connection_key", sa.String(length=2048), nullable=True),
    )
    targets = sa.table(
        "runtime_targets",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("connection_config", sa.JSON()),
        sa.column("connection_key", sa.String()),
    )
    connection = op.get_bind()
    owners: dict[str, str] = {}
    for target_id, name, config in connection.execute(
        sa.select(targets.c.id, targets.c.name, targets.c.connection_config)
    ):
        endpoint = _normalize(config["endpoint"])
        if endpoint in owners:
            raise RuntimeError(
                f"Runtime Targets {owners[endpoint]} and {name} share "
                f"endpoint {endpoint}. Disable and purge one before "
                "upgrading."
            )
        owners[endpoint] = name
        connection.execute(
            targets.update()
            .where(targets.c.id == target_id)
            .values(
                connection_config={"endpoint": endpoint},
                connection_key=endpoint,
            )
        )
    op.alter_column(
        "runtime_targets",
        "connection_key",
        existing_type=sa.String(length=2048),
        nullable=False,
    )
    op.create_unique_constraint(
        op.f("uq_runtime_targets_connection_key"),
        "runtime_targets",
        ["connection_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_runtime_targets_connection_key"),
        "runtime_targets",
        type_="unique",
    )
    op.drop_column("runtime_targets", "connection_key")
