"""Alembic environment.

Migrations run SYNCHRONOUSLY even though the application is async. psycopg3
supports both modes from the same URL, so this needs no separate driver and
no asyncio bridging inside the migration runner - Alembic's own machinery
stays entirely synchronous, which is one less thing to debug when a
migration fails.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import engine_from_config, pool

from config import settings
from database.base import Base

# Importing the models module is what populates Base.metadata. Without it
# autogenerate compares against an empty schema and cheerfully writes a
# migration that drops every table.
import database.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render `UtcDateTime` as the plain `sa.DateTime(timezone=True)` it
    compiles to.

    A migration is a historical record of what the schema was, and it has to
    keep running years later. If it imported `database.utc.UtcDateTime`, then
    renaming, moving, or deleting that class would break every migration that
    ever referenced it - and the fix would mean editing already-applied
    history. Rendering the underlying type keeps migrations dependent only on
    SQLAlchemy. The emitted DDL is identical either way: `TIMESTAMPTZ`.
    """
    if type_ == "type" and obj.__class__.__name__ == "UtcDateTime":
        # No imports.add() here: script.py.mako already imports sqlalchemy as
        # sa, and adding it again emits a duplicate import line.
        return "sa.DateTime(timezone=True)"
    return False  # fall through to Alembic's default rendering


def _database_url() -> str:
    """Resolve the URL, most explicit source first.

    `-x db_url=...` lets the test suite point at a scratch database it
    creates and drops, without touching .env or the tracked alembic.ini.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    return (
        x_args.get("db_url")
        or os.environ.get("ALEMBIC_DATABASE_URL")
        or settings.sync_database_url
    )


def assert_database_is_ours(connection) -> None:
    """Refuse to migrate a database this project does not own.

    THIS IS NOT PARANOIA. A native PostgreSQL install on the development
    machine listens on 5432 and holds the v1 project's live database -
    games, predictions, model_registry and a decade of ingested data. This
    project's Postgres is on 5433. The two differ by one character in a URL,
    and the failure mode is not an error: `alembic upgrade head` would
    happily add three tables to v1's schema, and `alembic downgrade base`
    would then drop them along with v1's alembic_version, leaving a live
    database in a state nothing knows how to repair.

    The test suite has its own guard, but that only protects the test path.
    This one protects the path that would actually cause the damage: a
    person at a terminal with a misconfigured .env. It lives in version
    control rather than in someone's shell history.

    The check is deliberately conservative: an empty database is fine (that
    is a first migration), and a database containing only our own tables is
    fine. Anything else stops.
    """
    inspector = sa.inspect(connection)
    present = set(inspector.get_table_names())
    if not present:
        return  # virgin database; this is a first migration

    ours = set(target_metadata.tables) | {"alembic_version"}
    foreign = present - ours
    if foreign:
        url = connection.engine.url
        raise SystemExit(
            "\nREFUSING TO MIGRATE: this database is not ours.\n\n"
            f"  target : {url.host}:{url.port}/{url.database}\n"
            f"  found  : {', '.join(sorted(foreign))}\n\n"
            "Those tables belong to another project. Migrating here could "
            "destroy live data.\n"
            "This project's database is on port 5433 "
            "(docker compose up -d postgres); port 5432 on this machine is a "
            "native PostgreSQL install holding the v1 project.\n"
            "Check DATABASE_URL in your .env.\n"
        )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # The ownership check runs on its OWN connection, which is then closed.
    #
    # This is not tidiness. Any query issued on the migration connection
    # before `context.begin_transaction()` implicitly opens a transaction;
    # Alembic then sees one already active, returns a no-op context manager
    # instead of its own, and never commits. The migration appears to
    # succeed - it logs "Running upgrade -> 0001" - and silently rolls back
    # when the connection closes, leaving an empty database and an alembic
    # version table that disagrees with reality.
    #
    # If you add another pre-flight check here, put it in this block too.
    with connectable.connect() as probe:
        assert_database_is_ours(probe)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Catch a column whose type drifted from the model, not just
            # added and dropped ones.
            compare_type=True,
            # Server defaults are deliberately NOT compared: they are
            # rendered inconsistently across Postgres versions ('now()' vs
            # CURRENT_TIMESTAMP, quoting of string literals), which produces
            # phantom diffs in `alembic check` that no edit can resolve.
            compare_server_default=False,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
