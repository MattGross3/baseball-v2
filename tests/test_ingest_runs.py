"""Ingest run lifecycle and the stale-run reaper."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

import database.ingest_run as ingest_run_module
from database.enums import IngestStatus
from database.ingest_run import (
    REAPED_ERROR,
    STALE_RUN_AFTER,
    ingest_run,
    reap_stale_runs,
    stale_runs,
)
from database.models import IngestRun
from database.utc import utcnow

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _reset_process_reap_flag():
    """The once-per-process latch would otherwise make test order matter."""
    ingest_run_module._reaped_this_process = False
    yield
    ingest_run_module._reaped_this_process = False


def _abandoned(*, age: dt.timedelta, source: str = "the-odds-api") -> IngestRun:
    """A run that started and never finished - what a crashed worker leaves."""
    return IngestRun(
        source=source,
        run_kind="odds_poll",
        status=IngestStatus.RUNNING.value,
        started_at=utcnow() - age,
    )


class TestReaper:
    async def test_reaps_a_run_that_outlived_any_plausible_execution(self, session):
        session.add(_abandoned(age=dt.timedelta(hours=6)))
        await session.commit()

        assert await reap_stale_runs(session) == 1
        await session.commit()

        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.status == IngestStatus.FAILED.value
        assert run.error == REAPED_ERROR
        assert run.finished_at is not None

    async def test_leaves_a_recent_run_alone(self, session):
        # A poll that started a minute ago is running, not dead.
        session.add(_abandoned(age=dt.timedelta(minutes=1)))
        await session.commit()

        assert await reap_stale_runs(session) == 0
        await session.commit()

        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.status == IngestStatus.RUNNING.value

    async def test_boundary_is_the_configured_age(self, session):
        session.add(_abandoned(age=STALE_RUN_AFTER + dt.timedelta(minutes=1)))
        session.add(_abandoned(age=STALE_RUN_AFTER - dt.timedelta(minutes=1)))
        await session.commit()

        assert await reap_stale_runs(session) == 1

    async def test_does_not_touch_finished_runs(self, session):
        # Only `running` rows are ambiguous. A failed run already told its
        # story and must keep its own error message.
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

    async def test_reaped_run_satisfies_the_coherence_constraint(self, session):
        # ck_ingest_runs_finished_iff_done and ck_ingest_runs_failed_has_error
        # both apply; a reaper that set status without finished_at or error
        # would raise here rather than silently producing an invalid row.
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

        # Reporting must not change what it reports on.
        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.status == IngestStatus.RUNNING.value


class TestAutomaticReapOnStartup:
    async def test_opening_a_run_reaps_abandoned_ones(self, session):
        """The reaper has to fire without anyone remembering to call it.

        ix_ingest_runs_running exists to find stuck rows; an index nothing
        queries is just a slower INSERT. Hooking the reap to the next writer
        is what turns "the poller has been broken for six days" from
        invisible into a failed run with a reason.
        """
        session.add(_abandoned(age=dt.timedelta(hours=6)))
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
        assert runs[0].error == REAPED_ERROR
        assert runs[1].status == IngestStatus.SUCCESS.value

    async def test_reaps_only_once_per_process(self, session):
        # A start-up concern, not a per-write cost: the second run in the
        # same process must not re-scan.
        session.add(_abandoned(age=dt.timedelta(hours=6)))
        await session.commit()

        async with ingest_run(session, source="manual-cli", run_kind="manual"):
            pass
        await session.commit()

        # A newly abandoned run appearing after the latch is set is NOT
        # reaped by a later writer in the same process - it will be caught
        # by the next process start.
        session.add(_abandoned(age=dt.timedelta(hours=6), source="other"))
        await session.commit()

        async with ingest_run(session, source="manual-cli", run_kind="manual"):
            pass
        await session.commit()

        still_running = await stale_runs(session)
        assert [r.source for r in still_running] == ["other"]
