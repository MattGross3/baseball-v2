"""Provenance: every row written gets traced back to the run that wrote it.

Used as a context manager so that a crash cannot leave a run unaccounted
for. On the way out the run is marked `success` or `failed` with the
exception text - a worker that dies mid-poll leaves evidence rather than
silence, and a row still in `running` long after it started is how a hung
process announces itself.
"""
from __future__ import annotations

import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from database.enums import IngestStatus
from database.models import IngestRun
from database.utc import utcnow

__all__ = ["ingest_run"]

# Postgres TEXT has no length limit, but an unbounded traceback in an error
# column makes `SELECT *` unreadable. Keep the tail, which holds the actual
# exception, rather than the head.
_MAX_ERROR_CHARS = 4000


@asynccontextmanager
async def ingest_run(
    session: AsyncSession,
    *,
    source: str,
    run_kind: str,
    params: dict | None = None,
) -> AsyncIterator[IngestRun]:
    """Open an `ingest_runs` row, yield it, and close it out.

    The caller is expected to set `rows_written` (and `api_requests`, where
    an external API was involved) on the yielded object before the block
    exits.
    """
    run = IngestRun(
        source=source,
        run_kind=run_kind,
        status=IngestStatus.RUNNING.value,
        started_at=utcnow(),
        params=params or {},
    )
    session.add(run)
    # Flush rather than commit: the run's id is needed immediately as a
    # foreign key for the rows about to be written, but the run and its rows
    # must land in the same transaction so a failure rolls back both.
    await session.flush()

    try:
        yield run
    except Exception as exc:
        # The transaction is poisoned at this point, so the failure cannot be
        # recorded on this session. Roll back, then write the failure row on
        # a fresh transaction - otherwise the crash that most needs recording
        # is the one that leaves no trace.
        await session.rollback()
        detail = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        failed = IngestRun(
            source=source,
            run_kind=run_kind,
            status=IngestStatus.FAILED.value,
            started_at=run.started_at,
            finished_at=utcnow(),
            params=params or {},
            error=detail[-_MAX_ERROR_CHARS:],
        )
        session.add(failed)
        await session.commit()
        raise
    else:
        run.status = IngestStatus.SUCCESS.value
        run.finished_at = utcnow()
        await session.flush()
