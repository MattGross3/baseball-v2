"""Ingest run lifecycle and the stale-run reaper."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import select, text

from database.enums import IngestStatus
from database.ingest_run import (
    PID_REAP_GRACE,
    REAPED_ERROR_BACKEND_GONE,
    REAPED_ERROR_NO_HEARTBEAT,
    STALE_RUN_AFTER,
    ingest_run,
    reap_stale_runs,
    stale_runs,
)
from database.models import IngestRun
from database.utc import utcnow

pytestmark = pytest.mark.postgres


def _abandoned(
    *,
    age: dt.timedelta,
    source: str = "the-odds-api",
    backend_pid: int | None = None,
) -> IngestRun:
    """A run that started and never finished - what a crashed worker leaves."""
    return IngestRun(
        source=source,
        run_kind="odds_poll",
        status=IngestStatus.RUNNING.value,
        started_at=utcnow() - age,
        backend_pid=backend_pid,
    )


class TestRunRowIsCommittedAtCreation:
    """The prerequisite that makes any reaper work at all.

    An earlier version only flushed the run row. That made it invisible to
    every other connection for the run's whole lifetime, and made it vanish
    entirely if the process died - so the reaper could never observe the one
    state it exists to clean up. Every row it had ever reaped was one a test
    inserted directly.
    """

    async def test_a_run_in_progress_is_visible_to_another_connection(
        self, session, database_url
    ):
        raw = database_url.replace("postgresql+psycopg://", "postgresql://")

        async with ingest_run(session, source="probe", run_kind="long") as run:
            with psycopg.connect(raw, autocommit=True) as conn:
                seen = conn.execute(
                    "SELECT status FROM ingest_runs WHERE id = %s", (run.id,)
                ).fetchone()
            # Visible while still running - this is what the reaper needs.
            assert seen is not None
            assert seen[0] == IngestStatus.RUNNING.value
            run.rows_written = 0
        await session.commit()

    async def test_the_run_records_the_backend_pid(self, session):
        async with ingest_run(session, source="probe", run_kind="long") as run:
            run.rows_written = 0
        await session.commit()

        stored = (await session.execute(select(IngestRun))).scalars().one()
        assert stored.backend_pid is not None
        assert stored.backend_pid > 0


class TestSuccessDoesNotInheritAReapError:
    """Step-0 regression.

    A long run can be reaped by another process while it is still working.
    When it then completes, it writes status='success' over the reaped row -
    and the reap's error text survived, leaving a run that reads as healthy
    while carrying a message saying it died.
    """

    async def test_reproduces_without_the_fix(self, session):
        # The raw sequence, bypassing ingest_run: reaped, then completed.
        # The CHECK constraint is what now makes this impossible.
        run = _abandoned(age=dt.timedelta(hours=6))
        session.add(run)
        await session.commit()

        assert await reap_stale_runs(session) == 1
        await session.commit()
        # reap_stale_runs issues a bulk UPDATE, which does not write through
        # to instances already loaded in the session - refresh to see what
        # the database actually holds.
        await session.refresh(run)
        assert run.error == REAPED_ERROR_NO_HEARTBEAT

        run.status = IngestStatus.SUCCESS.value
        run.finished_at = utcnow()
        # Deliberately NOT clearing error - the old success path did exactly
        # this, and nothing stopped it. Now the database does.
        with pytest.raises(sa_exc.IntegrityError, match="success_has_no_error"):
            await session.flush()
        await session.rollback()

    async def test_the_context_manager_clears_it(self, session):
        # End to end: a run reaped mid-flight still finishes clean.
        async with ingest_run(session, source="probe", run_kind="long") as run:
            run_id = run.id
            # Another process reaps it while it works. Force the reap to
            # apply by ageing the row past the fallback threshold and
            # clearing the pid, which is the "no heartbeat" path.
            await session.execute(
                text(
                    "UPDATE ingest_runs SET backend_pid = NULL, "
                    "started_at = :old WHERE id = :id"
                ),
                {"old": utcnow() - dt.timedelta(hours=6), "id": run_id},
            )
            await session.commit()
            assert await reap_stale_runs(session) == 1
            await session.commit()
            await session.refresh(run)
            assert run.status == IngestStatus.FAILED.value
            run.rows_written = 0

        await session.commit()
        stored = (await session.execute(select(IngestRun))).scalars().one()
        assert stored.status == IngestStatus.SUCCESS.value
        assert stored.error is None


class TestReaperByBackendLiveness:
    async def test_reaps_a_run_whose_backend_is_gone(self, session, database_url):
        """The demonstration: kill a real connection, watch the row get reaped.

        A wall-clock timeout can only guess whether a process is alive.
        pg_stat_activity knows.
        """
        raw = database_url.replace("postgresql+psycopg://", "postgresql://")

        # A separate connection stands in for another process's worker.
        victim = psycopg.connect(raw, autocommit=True)
        victim_pid = victim.execute("SELECT pg_backend_pid()").fetchone()[0]

        # Its run is older than the pid grace but far younger than the
        # one-hour age fallback - so if it gets reaped, liveness is why.
        age = PID_REAP_GRACE + dt.timedelta(minutes=1)
        assert age < STALE_RUN_AFTER
        session.add(_abandoned(age=age, backend_pid=victim_pid))
        await session.commit()

        # While the backend lives, the run is left alone.
        assert await reap_stale_runs(session) == 0
        await session.commit()

        # Now the process dies.
        victim.close()

        assert await reap_stale_runs(session) == 1
        await session.commit()

        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.status == IngestStatus.FAILED.value
        assert run.error == REAPED_ERROR_BACKEND_GONE
        assert run.finished_at is not None

    async def test_does_not_reap_inside_the_pid_grace(self, session, database_url):
        # Guards against pool churn at start-up producing a false positive:
        # a recorded pid can briefly not match the connection in use.
        raw = database_url.replace("postgresql+psycopg://", "postgresql://")
        victim = psycopg.connect(raw, autocommit=True)
        pid = victim.execute("SELECT pg_backend_pid()").fetchone()[0]
        victim.close()

        session.add(_abandoned(age=dt.timedelta(seconds=30), backend_pid=pid))
        await session.commit()

        # Backend is genuinely gone, but the run is too young to judge.
        assert await reap_stale_runs(session) == 0

    async def test_a_live_backend_is_never_reaped_however_old(
        self, session, database_url
    ):
        # The failure mode the timer had: a slow but healthy backfill got
        # marked failed just for taking longer than an hour.
        raw = database_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(raw, autocommit=True) as alive:
            pid = alive.execute("SELECT pg_backend_pid()").fetchone()[0]
            session.add(_abandoned(age=dt.timedelta(days=3), backend_pid=pid))
            await session.commit()

            assert await reap_stale_runs(session) == 0


class TestReaperByAgeFallback:
    async def test_reaps_a_pidless_run_that_is_old_enough(self, session):
        # Rows written before backend_pid existed, or by a writer that does
        # not hold a connection for the run's duration.
        session.add(_abandoned(age=dt.timedelta(hours=6), backend_pid=None))
        await session.commit()

        assert await reap_stale_runs(session) == 1
        await session.commit()

        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.status == IngestStatus.FAILED.value
        assert run.error == REAPED_ERROR_NO_HEARTBEAT

    async def test_leaves_a_recent_pidless_run_alone(self, session):
        session.add(_abandoned(age=dt.timedelta(minutes=1), backend_pid=None))
        await session.commit()
        assert await reap_stale_runs(session) == 0

    async def test_boundary_is_the_configured_age(self, session):
        session.add(_abandoned(age=STALE_RUN_AFTER + dt.timedelta(minutes=1)))
        session.add(_abandoned(age=STALE_RUN_AFTER - dt.timedelta(minutes=1)))
        await session.commit()
        assert await reap_stale_runs(session) == 1


class TestReaperScope:
    async def test_does_not_touch_finished_runs(self, session):
        finished = IngestRun(
            source="the-odds-api",
            run_kind="odds_poll",
            status=IngestStatus.FAILED.value,
            started_at=utcnow() - dt.timedelta(days=2),
            finished_at=utcnow() - dt.timedelta(days=2),
            error="upstream returned 503",
        )
        session.add(finished)
        await session.commit()

        assert await reap_stale_runs(session) == 0
        await session.commit()

        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.error == "upstream returned 503"

    async def test_reaped_run_satisfies_the_coherence_constraints(self, session):
        session.add(_abandoned(age=dt.timedelta(hours=6)))
        await session.commit()
        await reap_stale_runs(session)
        await session.commit()  # constraints are checked on write

    async def test_reaps_several_at_once(self, session):
        for _ in range(3):
            session.add(_abandoned(age=dt.timedelta(hours=6)))
        session.add(_abandoned(age=dt.timedelta(minutes=5)))
        await session.commit()
        assert await reap_stale_runs(session) == 3


class TestStaleRunsReport:
    async def test_lists_without_mutating(self, session):
        session.add(_abandoned(age=dt.timedelta(hours=6)))
        await session.commit()

        found = await stale_runs(session)
        assert len(found) == 1
        assert found[0].status == IngestStatus.RUNNING.value

        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.status == IngestStatus.RUNNING.value


class TestAutomaticReap:
    async def test_opening_a_run_reaps_abandoned_ones(self, session):
        """Runs on every ingest_run(), with no latch.

        The old once-per-process global existed because a wall-clock scan
        felt expensive. With liveness the query is a partial-index scan over
        a table normally holding no running rows, so there is nothing to
        schedule around.
        """
        session.add(_abandoned(age=dt.timedelta(hours=6), backend_pid=None))
        await session.commit()

        async with ingest_run(session, source="manual-cli", run_kind="manual") as r:
            r.rows_written = 0
        await session.commit()

        runs = (
            (await session.execute(select(IngestRun).order_by(IngestRun.id)))
            .scalars()
            .all()
        )
        assert runs[0].status == IngestStatus.FAILED.value
        assert runs[0].error == REAPED_ERROR_NO_HEARTBEAT
        assert runs[1].status == IngestStatus.SUCCESS.value

    async def test_every_run_reaps_not_just_the_first(self, session):
        # The behaviour the latch used to prevent.
        async with ingest_run(session, source="manual-cli", run_kind="manual"):
            pass
        await session.commit()

        session.add(_abandoned(age=dt.timedelta(hours=6), backend_pid=None))
        await session.commit()

        async with ingest_run(session, source="manual-cli", run_kind="manual"):
            pass
        await session.commit()

        assert await stale_runs(session) == []

    async def test_a_run_never_reaps_itself(self, session):
        # The reap fires after this run's own row is committed, so the
        # ordering has to keep it out of its own candidate set.
        async with ingest_run(session, source="manual-cli", run_kind="manual") as r:
            r.rows_written = 0
            assert r.status == IngestStatus.RUNNING.value
        await session.commit()

        stored = (await session.execute(select(IngestRun))).scalars().one()
        assert stored.status == IngestStatus.SUCCESS.value


class TestFailurePath:
    async def test_records_the_failure_on_the_same_row(self, session):
        class Boom(RuntimeError):
            pass

        async def run_that_dies() -> None:
            async with ingest_run(session, source="manual-cli", run_kind="manual"):
                raise Boom("upstream returned 503")

        with pytest.raises(Boom):
            await run_that_dies()

        await session.rollback()
        # One row, not two: an attempt happened once, and it failed.
        stored = (await session.execute(select(IngestRun))).scalars().all()
        assert len(stored) == 1
        assert stored[0].status == IngestStatus.FAILED.value
        assert "upstream returned 503" in stored[0].error
        assert stored[0].finished_at is not None
