"""Async engine and session management.

psycopg3 drives both the async application and Alembic's synchronous
migration runner from the same URL, so there is one connection string to
keep correct rather than two.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import settings

__all__ = ["make_engine", "make_sessionmaker", "session_scope", "get_engine"]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    """Build an async engine.

    The pool is deliberately bounded with a short checkout timeout. A pool
    that grows without limit turns "one query is hanging" into "the whole
    application is unresponsive, cause unknown"; a bounded pool with a 15s
    timeout turns the same fault into a fast, legible error naming the
    caller that could not get a connection.
    """
    options = {
        "echo": settings.sql_echo,
        "pool_size": settings.pool_size,
        "max_overflow": settings.pool_max_overflow,
        "pool_timeout": settings.pool_timeout_seconds,
        # Recycle before typical idle-connection reapers close them, so a
        # long-idle worker does not wake to a dead socket.
        "pool_pre_ping": True,
    }
    options.update(kwargs)
    return create_async_engine(url or settings.database_url, **options)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        # Attributes stay loaded after commit. Without this, reading any
        # field of a just-committed object triggers a refresh - which in
        # async code means an await in a place that looks synchronous, and
        # a confusing MissingGreenlet error.
        expire_on_commit=False,
    )


def get_engine() -> AsyncEngine:
    """Process-wide engine, created on first use."""
    global _engine, _sessionmaker
    if _engine is None:
        _engine = make_engine()
        _sessionmaker = make_sessionmaker(_engine)
    return _engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session wrapping one unit of work: commits on success, rolls back on
    any exception, and always closes."""
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the pool. For test teardown and clean process shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
