#!/usr/bin/env python3
"""Capture one Odds API slate to raw/. Standalone and disposable.

Deliberately dependency-free: stdlib only, no Postgres, no project imports,
no tests. It exists so price capture can start TONIGHT while the real
ingest is still being built - a credit not spent during a game is a closing
line that does not exist and cannot be bought back later.

    python scripts/dump_odds.py

Writes raw/odds_<UTC ISO8601 basic>.json and prints the timestamp and the
credits remaining. Cost: 2 credits (one per market: h2h, totals).

Captures produced here are the same envelope shape `ingestion/replay.py`
reads, so anything collected tonight can be loaded into Postgres later
without being re-fetched.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
MARKETS = "h2h,totals"
REGIONS = "us"
RAW_DIR = pathlib.Path(__file__).resolve().parent.parent / "raw"
TIMEOUT = 30


def _api_key() -> str:
    """ODDS_API_KEY from the environment, falling back to a .env file.

    The fallback is there because that is where the key already lives, and
    requiring an export before every manual run is how a capture gets
    skipped on the night it mattered.
    """
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if key:
        return key

    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "ODDS_API_KEY" and value.strip():
                return value.strip()

    sys.exit(
        "ODDS_API_KEY is not set.\n"
        "  PowerShell:  $env:ODDS_API_KEY = '...'\n"
        "  bash:        export ODDS_API_KEY=...\n"
        "  or put ODDS_API_KEY=... in .env at the repo root."
    )


def main() -> int:
    url = BASE_URL + "?" + urllib.parse.urlencode(
        {
            "apiKey": _api_key(),
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": "american",
        }
    )

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8")
            headers = dict(response.headers)
    except urllib.error.HTTPError as exc:
        # Loud, and without echoing the URL - it carries the key.
        sys.exit(f"HTTP {exc.code} from The Odds API: {exc.read()[:300]!r}")
    except urllib.error.URLError as exc:
        sys.exit(f"could not reach The Odds API: {exc.reason}")

    now = dt.datetime.now(dt.UTC)
    # Basic ISO 8601, no colons: a colon is illegal in a Windows filename.
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"odds_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "captured_at": now.isoformat(),
                "markets": MARKETS.split(","),
                "regions": REGIONS.split(","),
                # The credit accounting belongs with the payload: a bare
                # array on disk cannot say what it cost or what was left.
                "headers": {
                    k: v for k, v in headers.items() if k.lower().startswith("x-requests")
                },
                "events": json.loads(body),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    events = json.loads(body)
    print(f"captured   {now.isoformat()}")
    print(f"file       {path}")
    print(f"events     {len(events)}")
    print(f"cost       {headers.get('x-requests-last', '?')} credit(s)")
    print(f"remaining  {headers.get('x-requests-remaining', '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
