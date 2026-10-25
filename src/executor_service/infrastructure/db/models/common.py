"""Shared SQLAlchemy ORM column and constraint helpers."""

from datetime import UTC, datetime
from enum import Enum as PythonEnum

from sqlalchemy import CheckConstraint, DateTime, Dialect, Enum
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """``timestamptz`` that always reads back timezone-aware in UTC.

    PostgreSQL already returns aware values; SQLite, used by unit tests,
    drops the zone. Normalizing here keeps the rest of the code free of
    per-comparison fixes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def enum_type(enum_class: type[PythonEnum], name: str) -> Enum:
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=False,
        length=32,
    )


def audit_actor_constraints() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN "
            "('AGENT', 'USER', 'BATCH')",
            name="valid_created_by_type",
        ),
        CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN "
            "('AGENT', 'USER', 'BATCH')",
            name="valid_updated_by_type",
        ),
        CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name="complete_created_by",
        ),
        CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name="complete_updated_by",
        ),
    )
