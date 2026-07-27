"""Test fixtures.

The DB fixtures CREATE and DROP a database on every run, so they carry
deliberate guards. This machine has a native PostgreSQL 18 on port 5432
holding the v1 project's live data (games, predictions, model_registry, ...)
and this project's container on 5433. A misconfigured TEST_DATABASE_URL
pointing at the former would destroy real data, so `_guarded_test_url`
refuses to proceed unless the target both is named like a test database and
looks like one.

Schema is built by running the actual Alembic migration rather than
`metadata.create_all`. Those two can drift, and if they do, `create_all`
would hide it: the tests would pass against a schema that no deployment
ever has.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from config import settings
from database.enums import IngestStatus
from database.models import Bet, IngestRun, OddsSnapshot

# Tables this project owns. Anything else in the target database means the
# URL is pointing somewhere it should not be.
_OUR_TABLES = {"bets", "odds_snapshots", "ingest_runs", "alembic_version"}

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pytest_asyncio_loop_factories(config, item):
    """Run the async tests on a selector-based event loop.

    Windows defaults to ProactorEventLoop, which psycopg's async mode cannot
    use - every DB test fails with "Psycopg cannot use the
    'ProactorEventLoop'". The fix is a SelectorEventLoop.

    Done through this hook rather than by overriding the `event_loop_policy`
    fixture because the whole asyncio policy API is deprecated in Python 3.14
    and slated for removal in 3.16, and pytest-asyncio separately deprecates
    overriding that fixture. The loop CLASS is not deprecated - only the
    policy machinery around it - so selecting it directly is the option with
    no expiry date.
    """
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def _guarded_test_url() -> str:
    """Validate TEST_DATABASE_URL before anything destructive touches it."""
    url = make_url(settings.test_database_url)
    name = url.database or ""

    if not name.endswith("_test"):
        pytest.exit(
            f"TEST_DATABASE_URL names database {name!r}, which does not end in "
            "'_test'. This fixture DROPS the database it runs against; refusing "
            "to touch anything not explicitly marked as a test database.",
            returncode=1,
        )

    # Not fatal on its own - someone may legitimately run Postgres on 5432 -
    # but on this machine 5432 is the v1 project's live database, so it is
    # worth saying out loud rather than discovering afterwards.
    if url.port == 5432:
        pytest.exit(
            "TEST_DATABASE_URL points at port 5432, which on this machine is a "
            "native PostgreSQL install holding the v1 project's live data. This "
            "project's Postgres is on 5433 (docker compose up -d postgres).",
            returncode=1,
        )
    return settings.test_database_url


def _admin_dsn(url_str: str) -> str:
    """A libpq DSN for the 'postgres' maintenance database on the same server.

    CREATE/DROP DATABASE cannot run from inside the database being dropped.
    """
    url = make_url(url_str).set(database="postgres")
    return url.render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _assert_safe_to_drop(admin_dsn: str, dbname: str) -> None:
    """Refuse to drop a database that contains tables we do not recognise."""
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            return

    url = make_url(settings.test_database_url).set(database=dbname)
    dsn = url.render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        found = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
    unknown = found - _OUR_TABLES
    if unknown:
        pytest.exit(
            f"Refusing to drop database {dbname!r}: it contains tables this "
            f"project does not own ({sorted(unknown)}). That is somebody else's "
            "database.",
            returncode=1,
        )


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Create a freshly migrated test database; drop it when the run ends."""
    url_str = _guarded_test_url()
    url = make_url(url_str)
    dbname = url.database
    admin = _admin_dsn(url_str)

    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"Postgres unavailable at {url.host}:{url.port} "
            f"({exc.__class__.__name__}). "
            "Start it with: docker compose up -d postgres"
        )

    _assert_safe_to_drop(admin, dbname)

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{dbname}"')

    # Build the schema with the real migration, so a model/migration drift
    # breaks the whole suite rather than hiding behind create_all().
    cfg = Config(os.path.join(_PROJECT_ROOT, "alembic.ini"))
    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = url_str
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous

    yield url_str

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    from database.engine import make_engine

    eng = make_engine(database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A clean session per test.

    Isolation is by TRUNCATE rather than by wrapping each test in a
    transaction that gets rolled back. The rollback trick is faster, but
    `database.ingest_run` deliberately performs its own rollback-and-commit
    when recording a failure, which would tear the enclosing transaction out
    from under it and make that code untestable.
    """
    from database.engine import make_sessionmaker

    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE bets, odds_snapshots, ingest_runs RESTART IDENTITY CASCADE")
        )

    factory = make_sessionmaker(engine)
    async with factory() as s:
        yield s


# --- convenience builders -------------------------------------------------
# Fixed instants so tests read as a timeline rather than as arithmetic on
# datetime.now(). First pitch is 23:05Z on 2026-07-27; the venue-local date
# is the same day here, except where a test deliberately exercises the
# late-Pacific case where they differ.

FIRST_PITCH = dt.datetime(2026, 7, 27, 23, 5, tzinfo=dt.UTC)
GAME_DATE = dt.date(2026, 7, 27)
GAME_PK = 776543


@pytest.fixture
async def run(session: AsyncSession) -> IngestRun:
    """A completed ingest run to hang snapshots off.

    Committed, not just flushed: the constraint tests deliberately provoke
    IntegrityErrors and roll back to recover, and a merely-flushed run would
    disappear along with the failed statement.
    """
    ingest = IngestRun(
        source="test",
        run_kind="manual",
        status=IngestStatus.SUCCESS.value,
        started_at=FIRST_PITCH - dt.timedelta(hours=6),
        finished_at=FIRST_PITCH - dt.timedelta(hours=6),
    )
    session.add(ingest)
    await session.commit()
    return ingest


def make_snapshot(
    run: IngestRun,
    *,
    captured_at: dt.datetime,
    odds: int,
    game_pk: int = GAME_PK,
    game_date: dt.date = GAME_DATE,
    commence: dt.datetime = FIRST_PITCH,
    book: str = "pinnacle",
    market: str = "moneyline",
    selection: str = "away",
    line: Decimal | None = None,
) -> OddsSnapshot:
    return OddsSnapshot(
        ingest_run_id=run.id,
        game_pk=game_pk,
        game_date=game_date,
        commence_time_utc=commence,
        book=book,
        market=market,
        selection=selection,
        line=line,
        odds_american=odds,
        captured_at=captured_at,
    )


def make_bet(
    *,
    odds: int,
    game_pk: int = GAME_PK,
    game_date: dt.date = GAME_DATE,
    commence: dt.datetime = FIRST_PITCH,
    book: str = "pinnacle",
    market: str = "moneyline",
    selection: str = "away",
    line: Decimal | None = None,
    stake_cents: int = 5000,
    placed_at: dt.datetime | None = None,
) -> Bet:
    return Bet(
        game_pk=game_pk,
        game_date=game_date,
        commence_time_utc=commence,
        book=book,
        market=market,
        selection=selection,
        line=line,
        odds_american=odds,
        stake_cents=stake_cents,
        placed_at=placed_at or (commence - dt.timedelta(hours=5)),
    )
