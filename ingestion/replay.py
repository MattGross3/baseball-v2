"""Re-parse raw captures into snapshots.

The reason `capture_slate` writes to disk before parsing. A credit spent is
gone; a parse bug is not. This turns "the parser was wrong for three days"
from lost prices into a re-run.

Uses `parse_capture` - the SAME function the live poller uses, not a copy.
A parallel implementation would drift, and would drift precisely in the case
where it matters: a parser fix applied to live polling but not to the
backlog it was written to recover.

    python -m ingestion.replay                 # every capture in raw/
    python -m ingestion.replay raw/odds_X.json # one
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import pathlib
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import session_scope
from database.ingest_run import ingest_run
from ingestion.odds import (
    RAW_DIR,
    UnknownGame,
    _game_dates,
    load_game_index,
    parse_capture,
    store_snapshots,
)

__all__ = ["replay_all", "replay_capture"]

log = logging.getLogger(__name__)

SOURCE = "replay"


async def replay_capture(session: AsyncSession, path: pathlib.Path) -> int:
    """Re-parse one capture and store whatever it yields. Returns rows written.

    Idempotent by construction: `captured_at` comes from the capture's own
    envelope, not from now(), so re-running produces the identical natural
    key and ON CONFLICT DO NOTHING absorbs it. Replaying the same file twice
    writes nothing the second time.
    """
    capture = json.loads(path.read_text(encoding="utf-8"))
    captured_at = dt.datetime.fromisoformat(capture["captured_at"]).astimezone(dt.UTC)

    async with ingest_run(
        session,
        source=SOURCE,
        run_kind="odds_replay",
        params={"capture": path.name},
    ) as run:
        # Index around the capture's own date, not today's - an old capture
        # must resolve against the games that existed then.
        index = await load_game_index(session, captured_at.date())
        rows, unresolved = parse_capture(capture, index)
        game_dates = await _game_dates(session, {r["game_pk"] for r in rows})
        written = await store_snapshots(
            session,
            rows,
            run_id=run.id,
            captured_at=captured_at,
            game_dates=game_dates,
        )
        run.rows_written = written
        if unresolved:
            run.params = {**run.params, "unresolved": unresolved}
        # No API call: this spends nothing.
        run.api_requests = 0

    log.info("%s: %d parsed, %d written", path.name, len(rows), written)
    return written


async def replay_all(
    session: AsyncSession, raw_dir: pathlib.Path = RAW_DIR
) -> tuple[int, int]:
    """Replay every capture in `raw_dir`, oldest first.

    A capture that cannot be resolved is logged and skipped rather than
    aborting the batch - one game missing from `games` should not stop the
    other 400 captures from being recovered.
    """
    written = failed = 0
    for path in sorted(raw_dir.glob("odds_*.json")):
        try:
            written += await replay_capture(session, path)
        except (UnknownGame, KeyError) as exc:
            failed += 1
            log.warning("%s: %s", path.name, exc)
    return written, failed


async def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    async with session_scope() as session:
        if argv:
            for name in argv:
                await replay_capture(session, pathlib.Path(name))
        else:
            written, failed = await replay_all(session)
            print(f"replayed: {written} row(s) written, {failed} capture(s) failed")
    return 0


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    raise SystemExit(asyncio.run(_main(sys.argv[1:]), loop_factory=loop_factory))
