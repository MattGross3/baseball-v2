# baseball v2

Betting analytics for MLB. **Phase 0: measurement infrastructure, nothing
predictive.**

The premise is that a model is worth building only once you can tell whether
it has edge, and the fastest honest signal for that is closing line value —
whether the price you took beats the price the market settled on. Results
converge slowly; CLV converges fast. So the measurement loop comes first, and
the models come later.

## What exists

| | |
|---|---|
| `betting/odds.py` | American ↔ decimal ↔ implied-probability conversion |
| `betting/devig.py` | Power-method devigging (bisection on `sum(p**k) == 1`) |
| `betting/clv.py` | CLV: price-based and no-vig, plus point-in-time queries |
| `betting/settle.py` | Integer-cent payouts, `Decimal` with `ROUND_HALF_UP` |
| `betting/cli.py` | Log bets, settle them, read CLV — the only way in |
| `database/` | Three tables, async SQLAlchemy 2.0, Alembic |

## What does not exist yet

No ingest workers, no `games` table, no API, no frontend, no models, no
simulation, no bankroll or Kelly sizing, no parlays. `snapshot add` stands in
for the odds poller so the loop is exercisable end to end today.

## Quick start

Requires Docker and Python 3.13 (see `.python-version`).

```bash
cp .env.example .env
make install
make db-up          # Postgres 16 on localhost:5433
make migrate
make test
```

Then log a bet against prices you enter by hand:

```bash
C="--game-pk 776543 --game-date 2026-07-27 \
   --commence-time 2026-07-27T23:05:00Z --book pinnacle --market moneyline"

# An opening price, a closing price, and the opposing close so the
# no-vig metric is computable.
python -m betting.cli snapshot add $C --selection away --odds=+130 \
    --captured-at 2026-07-27T20:05:00Z
python -m betting.cli snapshot add $C --selection away --odds=+110 \
    --captured-at 2026-07-27T22:55:00Z
python -m betting.cli snapshot add $C --selection home --odds=-130 \
    --captured-at 2026-07-27T22:55:00Z

python -m betting.cli bet log $C --selection away --odds=+130 --stake 50.00
python -m betting.cli bet settle --id 1 --status won
python -m betting.cli bet clv --id 1
```

```
bet #1
  bet at      +130 (break-even 0.4348)
  closed at   +110 (2026-07-27 22:55:00Z)
  CLV (price) +9.524%
  CLV (no-vig) +1.981 pts   fair closing prob 0.4546 (power method)
  beat close  yes
```

Use `--odds=-150`, not `--odds -150`. Both work today, but the `=` form does
not depend on argparse's negative-number heuristic.

## Non-negotiable rules

These are from `claude.md` and are enforced by tests, constraints, or both.
Each exists because violating it produces a plausible wrong number rather
than an error.

**Devigging uses the power method.** Never multiplicative normalisation
(`p_i / sum(p)`). Multiplicative understates the favourite's fair
probability — on a `-150/+130` market, 0.5798 against the correct 0.5840 —
so a model backing favourites sees edge that is not there, systematically, on
every favourite it ever bets.

**Games are keyed by `game_pk` alone.** Never `(date, home, away)`:
doubleheaders share a date and both teams, and that key silently merges two
different games.

**`game_date` and `commence_time_utc` are different facts.** `game_date` is
the venue-LOCAL date, from StatsAPI's `officialDate`. A 10:10pm Pacific game
is 05:10 UTC the *next day*. Deriving one from the other drops every late
West Coast game from any slate-keyed query.

**All timestamps are timezone-aware UTC.** Enforced by the `UtcDateTime`
column type, which refuses naive datetimes. Postgres would not: `timestamptz`
silently reinterprets a naive value in the server's timezone and stores a
different instant.

**Odds are American integers at rest.** Conversion happens at function
boundaries only. A `CHECK` rejects the impossible `(-100, +100)` range,
because a value in that gap is a probability or a percentage that reached an
odds column by mistake and would otherwise imply a plausible-looking price.

**`odds_snapshots` is append-only.** No `UPDATE`, ever. The natural key is
unique and writers use `ON CONFLICT DO NOTHING`, which inserts or does not —
it never rewrites a row. An unchanged re-poll at a *new* time is still a real
observation and is kept.

## Layout

```
betting/     odds, devigging, CLV, settlement, CLI
database/    models, async engine, UTC type, Alembic migrations
tests/       327 tests; pure-logic tests need no database
docs/        phase-0 plan, known gaps
```

## Testing

The suite runs against **real Postgres 16**, not SQLite. `CHECK` enforcement,
`TIMESTAMPTZ` semantics, `NULLS NOT DISTINCT`, partial indexes and NULL
handling in btree indexes are all Postgres behaviours — a SQLite run would
report them passing while proving nothing about production.

```bash
make test        # everything
make test-fast   # pure logic only, no Docker needed
make verify      # what CI runs
```

Two test groups are worth knowing about:

- `@pytest.mark.performance` asserts on `EXPLAIN` output. A failure means
  **investigate the plan**, not loosen the assertion — the failure mode it
  guards is an index silently going unused, which no functional test can see
  because the query still returns correct rows.
- Devig constants are cross-checked against a 50-digit `Decimal` solver that
  imports none of this code. No test asserts the implementation equals
  itself.

## Safety

Two guards exist because this machine runs a second, unrelated PostgreSQL on
port 5432 holding another project's live data:

- `database/migrations/env.py` refuses to migrate any database containing
  tables this project does not own.
- `tests/conftest.py` refuses to run unless the target database is named
  `*_test` and contains only our tables. The fixtures DROP that database.

## Documentation

- [docs/phase-0-plan.md](docs/phase-0-plan.md) — the design, the decisions,
  and the three corrections found by measuring rather than reasoning.
- [docs/known-gaps.md](docs/known-gaps.md) — what is knowingly unfinished.
- `claude.md` — the project's standing rules.
