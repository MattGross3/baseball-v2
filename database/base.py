"""Declarative base and the metadata naming convention.

The naming convention exists so that every constraint and index has a
deterministic name derived from its table and columns. Without it, Postgres
invents names for unnamed constraints, Alembic autogenerate cannot match an
existing constraint to a model-side one, and `alembic check` reports drift
that is not real. It also means a downgrade can drop a constraint by name.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
