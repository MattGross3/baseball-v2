"""Provenance: every row written gets traced back to the run that wrote it.

Used as a context manager so a crash cannot leave a run unaccounted for. The
run row is COMMITTED at creation, before any work happens, and that is
load-bearing rather than incidental - see `ingest_run`.
"""

from __future__ import annotations

import datetime as dt
import logging
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, and_, case, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.enums import IngestStatus
from database.models import IngestRun
from database.utc import utcnow

__all__ = [
    "PID_REAP_GRACE",
    "STALE_RUN_AFTER",
    "ingest_run",
    "reap_stale_runs",
    "stale_runs",
]

log = logging.getLogger(__name__)

# Postgres TEXT has no length limit, but an unbounded traceback in an error
# column makes `SELECT *` unreadable. Keep the tail, which holds the actual
# exception, rather than the head.
_MAX_ERROR_CHARS = 4000

# Fallback for rows with no backend_pid. Longer than any plausible ingest.
STALE_RUN_AFTER = dt.timedelta(hours=1)

# Minimum age before a run is reaped on pid evidence alone. See the note on
# connection churn in `reap_stale_runs`.
PID_REAP_GRACE = dt.timedelta(minutes=5)

REAPED_ERROR_NO_HEARTBEAT = "reaped: no heartbeat"
REAPED_ERROR_BACKEND_GONE = "reaped: backend gone"


async def reap_stale_runs(
    session: AsyncSession,
    *,
    older_than: dt.timedelta = STALE_RUN_AFTER,
    pid_grace: dt.timedelta = PID_REAP_GRACE,
) -> int:
    """Mark abandoned `running` rows as failed. Returns how many were reaped.

    Two mechanisms, in order of preference:

    1. **Backend liveness.** A run records the Postgres backend pid that
       created it. If no such pid appears in pg_stat_activity, the
       connection is gone, which means the process holding it is gone. This
       is evidence rather than inference: a wall-clock timeout can only
       guess, and guesses wrong in both directions - it reaps a slow but
       healthy backfill, and waits an hour on a process that died instantly.

    2. **Age**, for rows with no backend_pid: written before this column
       existed, or by some future writer that does not hold a connection for
       the run's duration.

    ASSUMPTIONS, because both are violable:

    - *A run holds its connection for its duration.* True under
      `session_scope()`, where the session keeps working on the same pooled
      connection. If a writer commits, releases the connection, and later
      picks up a different one, the recorded pid can go stale while the run
      is alive. `pid_grace` is the guard: a run is never reaped on pid
      evidence until it is at least that old, so ordinary pool churn during
      start-up cannot produce a false positive.

    - *Pids are per-server.* A run started against the same database from a
      DIFFERENT host has a pid meaningful only on that host, and this query
      would judge it by the wrong machine's process table. Correct for
      single-node deployment, wrong the moment there are two. When that
      happens, add a host column and compare on (host, pid), or move to an
      advisory lock held for the run's duration.

    Deliberately does NOT touch `partial` or `failed` runs - only `running`
    ones.
    """
    now = utcnow()

    # The only part that has to be raw: pg_stat_activity is a system view
    # with no ORM mapping, and correlating it to ingest_runs.backend_pid is
    # the whole point of the check.
    backend_is_gone = text(
        "NOT EXISTS (SELECT 1 FROM pg_stat_activity a "
        "WHERE a.pid = ingest_runs.backend_pid)"
    )

    result = await session.execute(
        update(IngestRun)
        .where(
            IngestRun.status == IngestStatus.RUNNING.value,
            or_(
                and_(
                    IngestRun.backend_pid.is_not(None),
                    IngestRun.started_at < now - pid_grace,
                    backend_is_gone,
                ),
                and_(
                    IngestRun.backend_pid.is_(None),
                    IngestRun.started_at < now - older_than,
                ),
            ),
        )
        .values(
            status=IngestStatus.FAILED.value,
            finished_at=now,
            # Say which mechanism fired. "backend gone" is evidence;
            # "no heartbeat" is only inference, and the distinction matters
            # when reading back why a run was marked failed.
            error=case(
                (IngestRun.backend_pid.is_(None), REAPED_ERROR_NO_HEARTBEAT),
                else_=REAPED_ERROR_BACKEND_GONE,
            ),
        )
    )
    reaped = cast("CursorResult[Any]", result).rowcount or 0
    if reaped:
        log.warning(
            "Reaped %d ingest run(s) still marked running - a worker died "
            "without recording its failure.",
            reaped,
        )
    return reaped


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


async def _backend_pid(session: AsyncSession) -> int | None:
    """The pid of the Postgres backend this session is currently using."""
    try:
        return (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    except Exception:  # pragma: no cover - non-Postgres or a dead connection
        log.debug("could not read pg_backend_pid()", exc_info=True)
        return None


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
    an external API was involved) before the block exits.

    THE RUN ROW IS COMMITTED BEFORE ANY WORK HAPPENS. This looks like a
    transactional wart and is the opposite: an earlier version only flushed,
    which meant the row was invisible to every other connection for the
    run's whole lifetime and disappeared entirely if the process died. The
    reaper could therefore never see a crashed or hung run - the only state
    it exists to clean up was the one state it could not observe. Committing
    first is what makes a run externally visible while it is happening, and
    what makes it survive the crash that leaves it stranded.

    The work itself runs in the transaction that follows, so a failure still
    rolls back the rows written - just not the record that the attempt
    happened, which is the part worth keeping.
    """
    run = IngestRun(
        source=source,
        run_kind=run_kind,
        status=IngestStatus.RUNNING.value,
        started_at=utcnow(),
        params=params or {},
        backend_pid=await _backend_pid(session),
    )
    session.add(run)
    await session.commit()

    # Clear out anything a previous process abandoned. After the commit
    # above, so this run is never a candidate for its own reap.
    await reap_stale_runs(session)
    await session.commit()

    try:
        yield run
    except Exception as exc:
        # The transaction is poisoned, so roll back the work first, then
        # record the failure on the run row that already exists. No second
        # row: an attempt happened once, and it failed.
        await session.rollback()
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        run.status = IngestStatus.FAILED.value
        run.finished_at = utcnow()
        run.error = detail[-_MAX_ERROR_CHARS:]
        await session.commit()
        raise
    else:
        run.status = IngestStatus.SUCCESS.value
        run.finished_at = utcnow()
        # Clear any error inherited from a reap that fired while this run was
        # still working. ck_ingest_runs_success_has_no_error rejects the row
        # otherwise, so this is enforced rather than merely intended.
        run.error = None
        await session.flush()
