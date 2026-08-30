"""Shared SQLAlchemy ORM column and constraint helpers."""

from enum import Enum as PythonEnum

from sqlalchemy import CheckConstraint, Enum


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
