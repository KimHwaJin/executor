"""Rename the stored execution.started ``runtime.session_id`` to ``kernel_id``.

The public contract now names the Jupyter kernel ``kernel_id`` so it cannot be
read as the caller's ``X-Session-Id``. Stored history is rewritten so the
event API returns one shape. Pods still on 0015 keep publishing; the event
contract accepts the old key while they drain.

Revision ID: 0016
Revises: 0015
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_EVENTS = sa.table(
    "execution_events",
    sa.column("id", sa.Uuid()),
    sa.column("event_type", sa.String()),
    sa.column("payload", sa.JSON()),
)


def _rename(old: str, new: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(_EVENTS.c.id, _EVENTS.c.payload).where(
            _EVENTS.c.event_type == "execution.started"
        )
    ).all()
    for event_id, payload in rows:
        if isinstance(payload, str):
            payload = json.loads(payload)
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict) or old not in runtime:
            continue
        runtime = {
            (new if key == old else key): value
            for key, value in runtime.items()
        }
        connection.execute(
            _EVENTS.update()
            .where(_EVENTS.c.id == event_id)
            .values(payload={**payload, "runtime": runtime})
        )


def upgrade() -> None:
    _rename("session_id", "kernel_id")


def downgrade() -> None:
    _rename("kernel_id", "session_id")
