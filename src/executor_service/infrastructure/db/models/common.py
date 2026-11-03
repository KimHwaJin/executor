"""Shared SQLAlchemy ORM column and constraint helpers."""

from datetime import UTC, datetime
from enum import Enum as PythonEnum

from sqlalchemy import DateTime, Dialect, Enum
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
