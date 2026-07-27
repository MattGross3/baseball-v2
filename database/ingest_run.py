"""Provenance: every row written gets traced back to the run that wrote it.

Used as a context manager so that a crash cannot leave a run unaccounted
for. On the way out the run is marked `success` or `failed` with the
exception text - a worker that dies mid-poll leaves evidence rather than
silence, and a row still in `running` long after it started is how a hung
process announces itself.
"""

from __future__ import annotations

import datetime as dt
import logging
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.enums import IngestStatus
from database.models import IngestRun
from database.utc import utcnow

__all__ = ["STALE_RUN_AFTER", "ingest_run", "reap_stale_runs"]

log = logging.getLogger(__name__)

# Postgres TEXT has no length limit, but an unbounded traceback in an error
# column makes `SELECT *` unreadable. Keep the tail, which holds the actual
# exception, rather than the head.
_MAX_ERROR_CHARS = 4000

# Longer than any plausible ingest. A poll that has been "running" for an
# hour is not slow, it is dead.
STALE_RUN_AFTER = dt.timedelta(hours=1)

REAPED_ERROR = "reaped: no heartbeat"

# Reaping is a start-up concern, not a per-write one. This makes it happen
# once per process rather than on every ingest_run() call.
_reaped_this_process = False


async def reap_stale_runs(
    session: AsyncSession, *, older_than: dt.timedelta = STALE_RUN_AFTER
) -> int:
    """Mark abandoned `running` rows as failed. Returns how many were reaped.

    A crashed worker leaves its run in `running` forever. The partial index
    ix_ingest_runs_running exists to find those rows, but an index nothing
    queries is just a slower INSERT - so something has to actually look.

    Without this, "the odds poller has been broken for six days" is
    invisible: the last row still says `running`, which reads as healthy at
    a glance, and there is no failed run and no error message anywhere to
    contradict it. With it, the same situation surfaces as a failed run with
    a reason.

    Deliberately does NOT touch `partial` or `failed` runs - only `running`
    ones that have outlived any plausible execution.
    """
    cutoff = utcnow() - older_than
    result = await session.execute(
        update(IngestRun)
        .where(
            IngestRun.status == IngestStatus.RUNNING.value,
            IngestRun.started_at < cutoff,
        )
        .values(
            status=IngestStatus.FAILED.value,
            finished_at=utcnow(),
            error=REAPED_ERROR,
        )
    )
    # `rowcount` is on CursorResult, which is what an UPDATE returns; the
    # generic Result type does not declare it.
    reaped = cast("CursorResult[Any]", result).rowcount or 0
    if reaped:
        log.warning(
            "Reaped %d ingest run(s) still marked running after %s - a worker "
            "died without recording its failure.",
            reaped,
            older_than,
        )
    return reaped


async def reap_stale_runs_once(session: AsyncSession) -> int:
    """Reap on first call in this process, then no-op.

    Keeps the guarantee that every writer reaps at start-up without paying
    an UPDATE on every single ingest_run().
    """
    global _reaped_this_process
    if _reaped_this_process:
        return 0
    _reaped_this_process = True
    return await reap_stale_runs(session)


async def stale_runs(
    session: AsyncSession, *, older_than: dt.timedelta = STALE_RUN_AFTER
) -> list[IngestRun]:
    """Currently-stuck runs, for reporting without mutating anything."""
    cutoff = utcnow() - older_than
    return list(
        (
            await session.execute(
                select(IngestRun)
                .where(
                    IngestRun.status == IngestStatus.RUNNING.value,
                    IngestRun.started_at < cutoff,
                )
                .order_by(IngestRun.started_at)
            )
        )
        .scalars()
        .all()
    )


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
    # Clear out anything a previous process abandoned. Once per process, so
    # this is a start-up concern rather than a cost on every write.
    await reap_stale_runs_once(session)

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
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
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
