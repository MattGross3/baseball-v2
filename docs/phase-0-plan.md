<!--
Copied from the planning session that produced Phase 0
(C:\Users\Gamin\.claude\plans\goal-establish-the-persistence-inherited-river.md).

Kept as a design record, not a live spec. Where this document and the code
disagree, the code is authoritative - see section 10, "What changed from the
approved plan", which records the three corrections found while building.
-->

# Phase 0 — Persistence Layer + CLV Measurement Loop

## Context

The goal is measurement infrastructure before any modeling: a place to record bets,
an append-only record of observed prices, provenance for every ingest, and an
honest CLV calculation built on power-method devigging. Nothing predictive.

**The exploration result that reframes this plan:** the working directory
`baseball v2` contains exactly one file — [claude.md](claude.md). No code, no
`.git`, no `package.json`, no dependency file, no directories.

The React/TS/Tailwind frontend and Python/XGBoost backend do exist, but in a
sibling repo: `AI-Testing\baseball-predictor` (~9,270 LOC Python, ~3,077 LOC
TS/TSX, 16 tables, 10 Alembic revisions, 12 pytest files, working
docker-compose). There is also an older prototype at
`PRE-Grad\baseball\baseball-model` with a more complete odds/EV library.

Per your ruling, Phase 0 is **greenfield in `baseball v2`**. v1 stays where it is
as a reference to harvest from in a later phase.

---

## 1. What exists to reuse vs. what is new

### Reuse — port by hand, adapting to the new rules

| From v1 | What to take | What to change |
|---|---|---|
| `database/db.py` | Pool sizing (5/10/15s), the `checkout` event listener that warns at ≤3 free connections, `session_scope()` contextmanager shape | Sync → **async** (`async_sessionmaker`, `AsyncSession`) |
| `config.py` | `pydantic-settings` `BaseSettings` + `.env` pattern, `has_*_key` properties, blank `sqlalchemy.url` in `alembic.ini` resolved in `env.py` | New driver URL; drop odds/weather keys until ingest exists |
| `database/models.py` | `DeclarativeBase` + `Mapped[]`/`mapped_column` 2.0 style; `DateTime(timezone=True)` on every timestamp; `Date` for venue-local dates. The `ForwardPrediction` docstring is the right tone for append-only tables | Wide `odds_snapshots` → long/narrow (see §3) |
| `alembic.ini` + `database/migrations/env.py` | Whole file layout, `script_location = %(here)s/database/migrations` | Fresh revision chain from `0001` |
| `ingestion/api_budget.py` | Record-the-call-at-the-request-site discipline | Folds into `ingest_runs.api_requests` |
| `tests/test_backtest.py` | Class-grouped tests, `monkeypatch` on settings, the security-regression style (`test_never_leaks_the_actual_key_value`) | SQLite in-memory → **real Postgres** |
| `docker-compose.yml` | `postgres:16-alpine` service + healthcheck + one-shot `migrate` gate | Postgres service only in Phase 0 |

### Build new

`betting/` package entirely (`odds.py`, `devig.py`, `clv.py`, `settle.py`,
`cli.py`), the three tables, `database/utc.py`, and the test suite. **Nothing in
v1 does bet persistence** — there is no `bets`, `wagers`, or `bankroll` table
anywhere, and no bet-entry UI. This is net-new surface area, not a refactor.

### Explicitly do not port

`backtest/clv_tracker.py` — its `devig_two_way` is the multiplicative
normalization the constraints forbid. Replace, don't wrap.

---

## 2. What in v1 fights the target architecture

Ranked by how much pain they cause on a future port. Worth knowing now because
the Phase 0 schema is what determines whether these get inherited.

1. **`odds_snapshots` is wide** (`database/models.py:216-229`) — one row carries
   `moneyline_home`, `moneyline_away`, `run_line`, `total`, `over_odds`,
   `under_odds` together, with no `book` column at all. It cannot express
   per-book prices, cannot represent an N-way market, and cannot answer "latest
   snapshot before T for a given selection." **This is the single change that
   makes the rest of the phase possible.** The new table is long/narrow.

2. **Games matched by `(date, home_name, away_name)`** —
   `ingestion/odds_api.py:184-190`. Two defects: `scalar_one_or_none()` raises
   `MultipleResultsFound` on a doubleheader (aborting the whole odds ingest for
   the day), and it compares `commence.date()` — the **UTC** date — against
   `Game.date`, which is MLB's venue-local `officialDate`. A 10:10pm ET game is
   02:10Z the next day, so late West Coast games silently never match and their
   odds are dropped at `log.debug`. This is precisely both constraints you named.

3. **Multiplicative devig is the only devig that exists** —
   `backtest/clv_tracker.py:29-49`, called from `api/routers/games.py:275` and
   `:366`. There is no power method, no Shin's, no overround helper anywhere in
   either repo.

4. **Edge computed against raw vigged prices** — `backtest/backtest_engine.py`
   uses `american_to_implied_prob(odds)` directly for `p_implied`, so backtest
   ROI measures a different edge than the UI shows. Manufactured edge, exactly
   the failure mode the devig constraint exists to prevent.

5. **Sync SQLAlchemy throughout** — [claude.md](claude.md) specifies async. A
   whole-repo retrofit if ported later; free if started async now.

6. **`dt.date.today()` used 19×** across scheduler, routers, and scripts —
   server-local, neither ET nor venue-local. The frontend found and fixed this
   bug in `web/src/lib/date.ts`; the Python side never did.

7. **Single book** — `_extract_best_lines` takes `bookmakers[0]`
   (`ingestion/odds_api.py:148`) despite its name. No line shopping, and no
   per-book CLV is possible.

8. **Tests run SQLite, prod runs Postgres** — `CHECK` constraints and
   `TIMESTAMPTZ` semantics differ, so v1's constraint coverage is illusory.
   Your ruling on real Postgres for Phase 0 fixes this from the start.

9. **`game_id` means the internal surrogate PK everywhere**; `mlb_game_id` holds
   the real `gamePk`. Phase 0 deliberately diverges: **`game_pk` is the key
   directly**, no surrogate. A later port must reconcile this.

---

## 3. Full DDL

Target: Postgres 16. Expressed as SQL for review; implemented as SQLAlchemy 2.0
models in `database/models.py` with Alembic revision `0001`.

### `ingest_runs`

Provenance for everything else. **This table is mutable** (`running` →
`success`); only `odds_snapshots` is append-only.

```sql
CREATE TABLE ingest_runs (
    id            BIGSERIAL     PRIMARY KEY,
    source        TEXT          NOT NULL,   -- 'the-odds-api' | 'mlb-statsapi' | 'manual-cli'
    run_kind      TEXT          NOT NULL,   -- 'odds_poll' | 'schedule' | 'manual'
    status        TEXT          NOT NULL DEFAULT 'running',
    started_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ   NULL,
    rows_written  INTEGER       NOT NULL DEFAULT 0,
    api_requests  INTEGER       NOT NULL DEFAULT 0,
    params        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    error         TEXT          NULL,

    CONSTRAINT ck_ingest_runs_status
        CHECK (status IN ('running','success','partial','failed')),
    CONSTRAINT ck_ingest_runs_finished_iff_done
        CHECK ((status = 'running') = (finished_at IS NULL)),
    CONSTRAINT ck_ingest_runs_counts_nonneg
        CHECK (rows_written >= 0 AND api_requests >= 0),
    CONSTRAINT ck_ingest_runs_failed_has_error
        CHECK (status <> 'failed' OR error IS NOT NULL)
);

CREATE INDEX ix_ingest_runs_source_started
    ON ingest_runs (source, started_at DESC);
CREATE INDEX ix_ingest_runs_running
    ON ingest_runs (started_at) WHERE status = 'running';
```

**Index reasoning.** `(source, started_at DESC)` answers "when did odds last poll
successfully" in one index seek — the health question a scheduler asks constantly.
The partial index on `status='running'` is tiny (normally 0–2 rows) and finds
stuck runs; without it, detecting a crashed worker means a seq scan of all history.

### `odds_snapshots`

**Append-only. No UPDATE, no UPSERT, ever.** One row = one observed price for one
selection at one book at one instant.

```sql
CREATE TABLE odds_snapshots (
    id                 BIGSERIAL     PRIMARY KEY,
    ingest_run_id      BIGINT        NOT NULL REFERENCES ingest_runs(id),

    game_pk            INTEGER       NOT NULL,
    game_date          DATE          NOT NULL,   -- LOCAL date at venue (StatsAPI officialDate)
    commence_time_utc  TIMESTAMPTZ   NOT NULL,   -- as reported by source at capture time

    book               TEXT          NOT NULL,
    market             TEXT          NOT NULL,   -- 'moneyline' | 'total' | 'run_line'
    selection          TEXT          NOT NULL,   -- 'home' | 'away' | 'over' | 'under'
    line               NUMERIC(4,2)  NULL,       -- NULL iff market='moneyline'
    odds_american      INTEGER       NOT NULL,

    captured_at        TIMESTAMPTZ   NOT NULL,

    CONSTRAINT ck_odds_american_valid
        CHECK (odds_american <= -100 OR odds_american >= 100),
    CONSTRAINT ck_odds_market
        CHECK (market IN ('moneyline','total','run_line')),
    CONSTRAINT ck_odds_selection
        CHECK (selection IN ('home','away','over','under')),
    CONSTRAINT ck_odds_line_presence
        CHECK ((market =  'moneyline' AND line IS NULL)
            OR (market <> 'moneyline' AND line IS NOT NULL)),
    CONSTRAINT ck_odds_selection_matches_market
        CHECK ((market =  'total'     AND selection IN ('over','under'))
            OR (market <> 'total'     AND selection IN ('home','away'))),
    CONSTRAINT ck_odds_game_pk_positive
        CHECK (game_pk > 0)
);

CREATE INDEX ix_odds_snapshots_pit
    ON odds_snapshots (game_pk, market, selection, book, captured_at, id);
CREATE INDEX ix_odds_snapshots_game_captured
    ON odds_snapshots (game_pk, captured_at);
CREATE INDEX ix_odds_snapshots_ingest_run_id
    ON odds_snapshots (ingest_run_id);
```

> **CORRECTED DURING IMPLEMENTATION.** The plan originally put `line` in
> `ix_odds_snapshots_pit`, between `book` and `captured_at`. Measured against
> 400k rows, that was wrong and would have cost a sort on every moneyline
> lookup. See "What changed" at the end of this document.

**Index reasoning — this is the one you care about.** The target query:

```sql
SELECT odds_american, captured_at FROM odds_snapshots
WHERE game_pk = $1 AND market = $2 AND selection = $3 AND book = $4
  AND line IS NULL                    -- or `line = $5` for total/run_line
  AND captured_at <= $6
ORDER BY captured_at DESC
LIMIT 1;
```

`ix_odds_snapshots_pit` puts all five equality predicates first, in fixed order,
so the index narrows to one contiguous range; `captured_at` trails so the
`<= T ORDER BY DESC LIMIT 1` is satisfied by walking that range and stopping at
the first row. Cost is `O(log n)` plus one tuple, and it stays that way as the
table grows without bound — which it will, since nothing is ever deleted.

Four notes on this index, in decreasing order of how much they matter:

- **`line` is deliberately NOT in the index.** A btree yields ordered output on
  a trailing column only when every preceding column is bound by *equality*.
  `line IS NULL` — which is every moneyline row, the most common market — is a
  valid scan key but not an equality for pathkey purposes. Putting `line` ahead
  of `captured_at` therefore destroys the ordering guarantee: Postgres reads
  every matching row and sorts. Measured on 400k rows this turned a 0.04ms
  ordered scan into a bitmap scan plus top-N sort at ~13× the estimated cost.
  Left out, `line` is a cheap filter and ordering holds for every market.
- **`id` trails `captured_at`** because the query breaks ties on it, so two
  prices stamped at the same instant resolve identically on every call. Without
  `id` in the index, Postgres bolts on an Incremental Sort.
- **`line` must still be queried as `IS NULL`, not `IS NOT DISTINCT FROM`.**
  Postgres cannot use a btree index for `IS NOT DISTINCT FROM` at all. The app
  emits `OddsSnapshot.line.is_(None)` for moneyline and `== line` otherwise.
- Declared ASC, not DESC: Postgres scans a btree backward at the same cost, and
  a DESC declaration would make it an index Alembic compares poorly.
- `ix_odds_snapshots_game_captured` is partly redundant with the prefix of the
  first index, but the first only orders by time *within* a fixed
  market/selection/book. Whole-game time-ordered scans (line movement across
  every market) need this one. It is cheap; drop it if it goes unused.

`ix_odds_snapshots_ingest_run` exists because Postgres does **not** auto-index FK
columns, and "what did run 47 write" is the first question asked when an ingest
looks wrong.

**No unique constraint on the natural key** — that is the append-only guarantee.
Two identical prices at different `captured_at` are two legitimate observations.
See Decision D3.

**`game_date` is not derivable here.** The odds feed gives `commence_time` in UTC
only. `game_date` must come from StatsAPI's `officialDate`, supplied by the
caller. Deriving it as `commence_time_utc.date()` is v1's bug at
`ingestion/odds_api.py:189` and is forbidden. `NOT NULL` enforces that the future
odds worker resolves the schedule first — correct ordering anyway.

### `bets`

```sql
CREATE TABLE bets (
    id                 BIGSERIAL     PRIMARY KEY,

    game_pk            INTEGER       NOT NULL,
    game_date          DATE          NOT NULL,   -- LOCAL date at venue
    commence_time_utc  TIMESTAMPTZ   NOT NULL,   -- CLV cutoff

    book               TEXT          NOT NULL,
    market             TEXT          NOT NULL,
    selection          TEXT          NOT NULL,
    line               NUMERIC(4,2)  NULL,
    odds_american      INTEGER       NOT NULL,
    stake_cents        BIGINT        NOT NULL,

    placed_at          TIMESTAMPTZ   NOT NULL,
    status             TEXT          NOT NULL DEFAULT 'open',
    settled_at         TIMESTAMPTZ   NULL,
    payout_cents       BIGINT        NULL,       -- total returned, incl. stake

    model_prob         NUMERIC(8,6)  NULL,       -- nothing fills this in Phase 0
    notes              TEXT          NULL,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT ck_bets_odds_american_valid
        CHECK (odds_american <= -100 OR odds_american >= 100),
    CONSTRAINT ck_bets_stake_positive     CHECK (stake_cents > 0),
    CONSTRAINT ck_bets_payout_nonneg      CHECK (payout_cents IS NULL OR payout_cents >= 0),
    CONSTRAINT ck_bets_game_pk_positive   CHECK (game_pk > 0),
    CONSTRAINT ck_bets_status
        CHECK (status IN ('open','won','lost','push','void')),
    CONSTRAINT ck_bets_market
        CHECK (market IN ('moneyline','total','run_line')),
    CONSTRAINT ck_bets_selection
        CHECK (selection IN ('home','away','over','under')),
    CONSTRAINT ck_bets_line_presence
        CHECK ((market =  'moneyline' AND line IS NULL)
            OR (market <> 'moneyline' AND line IS NOT NULL)),
    CONSTRAINT ck_bets_selection_matches_market
        CHECK ((market =  'total'     AND selection IN ('over','under'))
            OR (market <> 'total'     AND selection IN ('home','away'))),
    CONSTRAINT ck_bets_settlement_coherent
        CHECK ((status =  'open' AND settled_at IS     NULL AND payout_cents IS     NULL)
            OR (status <> 'open' AND settled_at IS NOT NULL AND payout_cents IS NOT NULL)),
    CONSTRAINT ck_bets_model_prob_range
        CHECK (model_prob IS NULL OR (model_prob > 0 AND model_prob < 1))
);

CREATE INDEX ix_bets_game_pk    ON bets (game_pk);
CREATE INDEX ix_bets_game_date  ON bets (game_date);
CREATE INDEX ix_bets_open       ON bets (placed_at DESC) WHERE status = 'open';
```

**Index reasoning.** `game_pk` is the join key to `odds_snapshots` for CLV.
`game_date` serves day-level P&L rollups. The partial index on `status='open'` is
the settle workflow's query ("what's outstanding") and stays small forever, since
open bets are a bounded working set while settled bets grow without bound.

**Constraint reasoning.** `ck_*_odds_american_valid` is the important one — American
odds are never `0` and never fall strictly between `-100` and `+100`. A `50` in
that column is a units/percent value that leaked in from the wrong place, and it
would silently produce a plausible-looking implied probability. Catching it at
the DB is the difference between a loud failure and a corrupted CLV series.
`ck_bets_settlement_coherent` makes "settled" mean one thing instead of three.

Deliberately **not** constrained: `placed_at < commence_time_utc`. It is true for
every bet in Phase 0, but baking it in forecloses live betting for no benefit now.

---

## 4. Function signatures

### `betting/odds.py` — conversions at the boundary

A third module beyond the two you named. `devig.py` and `clv.py` both need these,
and putting them in `devig.py` would make `clv.py` import its conversion helpers
from a devigging module. Everything here is pure and stateless.

```python
def validate_american(odds: int) -> int
def american_to_decimal(odds: int) -> float
def decimal_to_american(decimal_odds: float) -> int
def american_to_implied_prob(odds: int) -> float
def implied_prob_to_american(prob: float) -> int
def profit_multiple(odds: int) -> float        # net profit per 1 unit staked
```

### `betting/devig.py` — power method

```python
@dataclass(frozen=True, slots=True)
class DevigResult:
    fair_probs: tuple[float, ...]
    raw_probs:  tuple[float, ...]
    k:          float
    overround:  float          # sum(raw_probs) - 1.0
    iterations: int

def overround(raw_probs: Sequence[float]) -> float

def devig_power(
    raw_probs: Sequence[float],
    *,
    tol: float = 1e-12,
    max_iter: int = 200,
) -> DevigResult

def devig_american(
    odds: Sequence[int],
    *,
    tol: float = 1e-12,
    max_iter: int = 200,
) -> DevigResult

def fair_probability(odds: Sequence[int], index: int) -> float
def fair_odds_american(odds: Sequence[int]) -> tuple[int, ...]

def _solve_k(raw_probs: Sequence[float], tol: float, max_iter: int) -> tuple[float, int]
```

**Solver.** `f(k) = sum(p_i**k) - 1` is strictly decreasing in `k` for all
`p_i ∈ (0,1)`, since `d/dk p**k = p**k · ln(p) < 0`. Monotone ⇒ bisection is
guaranteed to converge, needs no derivative, and has no failure mode. Bracket
from `k=1`: if `f(1) > 0` (overround) double the upper bound until `f < 0`; if
`f(1) < 0` (underround/arb) halve the lower bound until `f > 0`. ~50 iterations
for `1e-12`. No scipy, no numpy required.

**Rejected inputs** (`ValueError`): fewer than 2 probabilities; any `p ≤ 0` or
`p ≥ 1` (`p = 1` breaks the solver outright, since `1**k = 1` for all `k`); any
NaN.

Generic over N. Two-way is not special-cased — a three-way market and a
five-runner market use the same code path.

### `betting/clv.py`

```python
@dataclass(frozen=True, slots=True)
class ClvResult:
    bet_id:                  int | None
    bet_odds_american:       int
    closing_odds_american:   int
    closing_captured_at:     datetime
    clv_pct:                 float          # (dec_bet / dec_close - 1) * 100
    clv_prob_points:         float | None   # (fair_close - breakeven_bet) * 100
    fair_closing_prob:       float | None
    bet_breakeven_prob:      float
    beat_close:              bool

def opposite_selection(market: str, selection: str) -> str

def compute_clv(                                   # PURE — no session, no I/O
    *,
    bet_odds_american: int,
    closing_odds_american: int,
    closing_captured_at: datetime,
    opposing_closing_odds_american: int | None = None,
    bet_id: int | None = None,
) -> ClvResult

async def find_closing_snapshot(
    session: AsyncSession,
    *,
    game_pk: int,
    market: str,
    selection: str,
    book: str,
    line: Decimal | None,
    before: datetime,
) -> OddsSnapshot | None

async def find_opposing_closing_snapshot(
    session: AsyncSession, *, closing: OddsSnapshot
) -> OddsSnapshot | None

async def compute_clv_for_bet(
    session: AsyncSession, bet_id: int
) -> ClvResult | None
```

The pure/impure split mirrors [claude.md](claude.md)'s `simulate()` rule: the
arithmetic never touches a session, so every CLV property is testable without a
database. `clv_prob_points` is `None` when the opposing closing price was never
captured — you cannot devig one side of a market, and returning a plausible
number there would be a fabrication.

### `betting/settle.py`

```python
def payout_cents(stake_cents: int, odds_american: int, status: BetStatus) -> int
```

`won` → stake + round(stake × profit_multiple); `push`/`void` → stake; `lost` → 0.
`Decimal` with `ROUND_HALF_UP`, never float.

### `database/`

```python
# database/base.py
class Base(DeclarativeBase): ...

# database/utc.py
class UtcDateTime(TypeDecorator[datetime]):
    """TIMESTAMPTZ that refuses naive datetimes on the way in and
    guarantees tzinfo=UTC on the way out."""
    impl = DateTime(timezone=True)
    cache_ok = True

# database/engine.py
def make_engine(url: str | None = None) -> AsyncEngine
def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]
@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]

# database/enums.py
class Market(StrEnum):       MONEYLINE, TOTAL, RUN_LINE
class Selection(StrEnum):    HOME, AWAY, OVER, UNDER
class BetStatus(StrEnum):    OPEN, WON, LOST, PUSH, VOID
class IngestStatus(StrEnum): RUNNING, SUCCESS, PARTIAL, FAILED

# database/models.py
class IngestRun(Base): ...
class OddsSnapshot(Base): ...
class Bet(Base): ...

# database/ingest_run.py
@asynccontextmanager
async def ingest_run(
    session: AsyncSession, *, source: str, run_kind: str, params: dict | None = None
) -> AsyncIterator[IngestRun]
```

`UtcDateTime` on every timestamp column is what makes the UTC rule enforceable
rather than aspirational — a single `TypeDecorator` beats per-column validators
because it cannot be forgotten on a new column. `ingest_run()` opens a `running`
row, and on exit marks `success` or `failed` with the traceback, so a crashed
worker leaves evidence instead of silence.

### `betting/cli.py`

```
python -m betting.cli bet log --game-pk 776543 --game-date 2026-07-27 \
    --commence-time 2026-07-27T23:05:00Z --book pinnacle \
    --market moneyline --selection home --odds=-150 --stake 50.00 \
    [--placed-at ...] [--model-prob 0.62] [--notes "..."]

python -m betting.cli bet settle --id 12 --status won [--payout 83.33]
python -m betting.cli bet clv    --id 12
python -m betting.cli bet list   [--status open] [--game-date 2026-07-27]

python -m betting.cli snapshot add --game-pk 776543 --game-date 2026-07-27 \
    --commence-time 2026-07-27T23:05:00Z --book pinnacle \
    --market moneyline --selection home --odds=-150 --captured-at ...

python -m betting.cli devig --odds=-150 --odds=+130
```

`argparse` (stdlib, no new dependency). `snapshot add` exists so CLV is testable
end-to-end before any ingest worker — without it, Phase 0 ships a CLV function
with no way to feed it. Each CLI invocation opens its own `ingest_run` with
`source='manual-cli'`, so hand-entered prices carry the same provenance as
scraped ones.

Handlers are thin wrappers over plain functions (`log_bet(session, **kwargs)`),
so tests call the function, not a subprocess.

---

## 5. Test list

Structure: `tests/test_odds.py`, `test_devig.py`, `test_clv_math.py` (pure, no
DB), `test_clv_queries.py`, `test_schema.py`, `test_migrations.py`,
`test_cli.py` (Postgres). Pure tests run always; DB tests use a session-scoped
throwaway Postgres 16 database.

### `test_odds.py` — conversions

| # | Test | Property asserted |
|---|---|---|
| 1 | `test_negative_american_implied` | `-150` → `150/250` = **0.6 exactly** |
| 2 | `test_positive_american_implied` | `+130` → `100/230` = **0.434782608696** |
| 3 | `test_american_to_decimal` | `-150` → **1.666667**; `+130` → **2.30 exactly** |
| 4 | `test_decimal_american_roundtrip` | For every American int in ±[100, 2000], `decimal_to_american(american_to_decimal(x)) == x` — exact, not approximate |
| 5 | `test_profit_multiple` | `-150` → **0.666667**; `+130` → **1.30** |
| 6 | `test_rejects_invalid_american` | `0`, `50`, `-50`, `-99`, `99` each raise `ValueError`. Guards the same class of bad value the DB `CHECK` catches |
| 7 | `test_even_money_symmetry` | `+100` and `-100` both → decimal `2.0`, prob `0.5` |

### `test_devig.py` — power method

Tolerance discipline: closed-form cases assert to `1e-12`; numerically-solved
cases carry hand values good to `1e-4`, derived below by log/exp arithmetic
independent of the implementation. **No test asserts the implementation equals
itself.**

| # | Test | Property asserted |
|---|---|---|
| 8 | `test_symmetric_market_exact` | `[-110, -110]`: raw = `11/21` each (`0.5238095`), sum `22/21`. Closed form: `(11/21)**k = 1/2` ⇒ `k = ln(0.5)/ln(11/21)` = **1.0719426**. Result exactly **(0.5, 0.5)** to `1e-12` |
| 9 | `test_round_trip_from_constructed_book` | **Strongest test.** Pick fair `p = (0.5, 0.3, 0.2)` and `k = 1.05`; construct `q_i = p_i ** (1/1.05)` = `(0.516779, 0.317673, 0.215932)`, sum `1.050384`. Assert `devig_power(q)` recovers `p` to `1e-10` and `k = 1.05` to `1e-10`. Exact by construction — the inverse operation, not the implementation |
| 10 | `test_known_two_way_hand_computed` | `[-150, +130]`: raw `(0.6, 10/23)`, sum `1.0347826`. Solving `0.6**k + (10/23)**k = 1` gives **k ≈ 1.052970**, fair ≈ **(0.583983, 0.416017)**, tol `1e-4` |
| 11 | `test_differs_from_multiplicative` | Same market. Multiplicative gives `(0.579832, 0.420168)`. Assert power differs by `> 1e-3`, **and specifically that power assigns the favorite the *higher* fair probability** (`0.583983 > 0.579832`). This pins the direction: multiplicative understates the favorite's fair price, so a model backing favorites sees inflated edge. The regression test for the constraint |
| 12 | `test_probs_always_sum_to_one` | Over a grid of constructed overround books (N ∈ 2..6, overround 0.5%–15%), `sum(fair) == 1` within `1e-12` |
| 13 | `test_preserves_ordering` | `argsort(raw) == argsort(fair)` — devigging never reorders selections |
| 14 | `test_shrinks_longshots_more` | For any asymmetric book, `fair_i / raw_i` is monotonically increasing in `raw_i`: the favorite loses proportionally less than the longshot. The mechanism behind #11, stated as a property |
| 15 | `test_zero_vig_is_identity` | `[0.5, 0.5]` → `k == 1.0` exactly, probs unchanged, `overround == 0.0` |
| 16 | `test_underround_gives_k_below_one` | Arb book `[+105, +105]`: raw `0.4878` each, sum `0.9756 < 1`. Assert `k < 1`, result sums to 1. Proves the solver brackets downward, not just up |
| 17 | `test_extreme_favorite_stable` | `[-2000, +1200]`: raw `(0.952381, 0.076923)`, sum `1.029304`. Converges, no overflow, sums to 1. Guards numerical stability as `p → 1` |
| 18 | `test_n_way_generality` | A 4-selection book devigs correctly; no code path assumes 2 |
| 19 | `test_rejects_invalid_probs` | `p ≤ 0`, `p ≥ 1` (esp. exactly `1.0`, which makes `f(k)` constant), `len < 2`, `NaN` → `ValueError` |
| 20 | `test_overround_value` | `[-150, +130]` → `overround == 0.0347826` to `1e-9` |
| 21 | `test_fair_odds_american_consistent` | `fair_odds_american([-150, +130])` → ints whose implied probs sum to `1.0 ± 0.005` (integer-rounding slack), and whose ordering matches the input |

### `test_clv_math.py` — pure CLV

| # | Test | Property asserted |
|---|---|---|
| 22 | `test_clv_positive_when_line_shortens` | Bet `+130` (dec 2.30), close `+110` (dec 2.10). `clv_pct = (2.30/2.10 - 1)×100` = **+9.52381%**. `beat_close is True` |
| 23 | `test_clv_negative_when_line_lengthens` | Bet `+110`, close `+130`: **−8.69565%**. `beat_close is False` |
| 24 | `test_clv_zero_when_unchanged` | Identical odds → `clv_pct == 0.0` exactly; `beat_close is False` (strict `>`, not `>=`) |
| 25 | `test_clv_prob_points_hand_computed` | Bet away `+130` (breakeven `0.4347826`). Close: home `-160`, away `+140` → raw `(0.6153846, 0.4166667)`, sum `1.0320513`. Power gives **k ≈ 1.049138**, fair away ≈ **0.399122**. So `clv_prob_points = (0.399122 − 0.434783)×100` = **−3.5661**, tol `1e-3`. Cross-check: `clv_pct = (2.30/2.40 − 1)×100 = −4.1667%` — **both metrics agree in sign**, which is the real assertion |
| 26 | `test_clv_prob_points_none_without_opposing` | Omit `opposing_closing_odds_american` → `clv_prob_points is None` and `fair_closing_prob is None`, while `clv_pct` is still computed. You cannot devig half a market |
| 27 | `test_clv_uses_power_not_multiplicative` | For the #25 market, multiplicative would give fair away `0.4166667/1.0320513 = 0.403727` and thus `−3.1055` points. Assert the result matches the power value, not that one |
| 28 | `test_clv_rejects_invalid_odds` | Either leg at `0`/`50` → `ValueError`, not a silently wrong number |
| 29 | `test_compute_clv_is_pure` | Signature takes no session; calling it twice with identical args returns equal results and touches no I/O |

### `test_clv_queries.py` — point-in-time resolution (Postgres)

| # | Test | Property asserted |
|---|---|---|
| 30 | `test_picks_latest_before_cutoff` | Snapshots at T−3h, T−1h, **T+1h** (a live in-game price). Assert the **T−1h** row is returned. The core point-in-time guarantee |
| 31 | `test_respects_book` | Two books, same game/market/selection, different prices. A bet at `pinnacle` resolves against `pinnacle`'s close, never the other book's |
| 32 | `test_respects_line` | Totals snapshots at `8.5` and `9.0`. A bet on `over 8.5` resolves only against `8.5` rows — a line move is a different market, not a price move |
| 33 | `test_moneyline_null_line_matches` | Moneyline rows have `line IS NULL`; the query uses `.is_(None)` and finds them. Directly guards the `IS NOT DISTINCT FROM` index trap from §3 |
| 34 | `test_doubleheader_isolation` | Two games, **same date, same teams, different `game_pk`**, each with snapshots. A bet on game A never picks up game B's close. The constraint stated as a test |
| 35 | `test_none_when_no_prior_snapshot` | Only post-cutoff rows exist → `find_closing_snapshot` returns `None`; `compute_clv_for_bet` returns `None` rather than raising |
| 36 | `test_opposing_snapshot_resolution` | `find_opposing_closing_snapshot` picks the same game/book/line/time-bucket but the opposite selection — `home`↔`away`, `over`↔`under` |
| 37 | `test_uses_the_index` | `EXPLAIN` the target query and assert the plan contains `ix_odds_snapshots_pit` and no `Seq Scan`. Catches an index silently going unused after a predicate is reformulated |

### `test_schema.py` — constraints and time semantics (Postgres)

| # | Test | Property asserted |
|---|---|---|
| 38 | `test_rejects_naive_datetime` | Inserting a naive `datetime` into any timestamp column raises before it reaches the DB (`UtcDateTime`) |
| 39 | `test_returns_utc_aware` | A round-tripped timestamp comes back with `tzinfo` set and equal to UTC |
| 40 | `test_game_date_is_venue_local_not_utc` | A 10:10pm PT game: `commence_time_utc = 2026-07-28T05:10Z`, `game_date = 2026-07-27`. Assert both persist and **disagree by one day**, and that `game_date != commence_time_utc.date()`. This is v1's `odds_api.py:189` bug encoded as a test |
| 41 | `test_american_odds_check_constraint` | `odds_american` of `0`, `50`, `-99` each raise `IntegrityError` from Postgres — proving the constraint is real, not just app-level |
| 42 | `test_line_presence_constraint` | Moneyline with a non-null `line`, and total with a null `line`, both rejected |
| 43 | `test_selection_matches_market_constraint` | `market='total', selection='home'` rejected; `market='moneyline', selection='over'` rejected |
| 44 | `test_settlement_coherence_constraint` | `status='won'` with `settled_at IS NULL` rejected; `status='open'` with a `payout_cents` rejected |
| 45 | `test_append_only_allows_duplicate_natural_key` | Two rows identical except `captured_at` both persist. Asserts the **absence** of a unique constraint — an unchanged re-poll is a real observation, not a conflict |
| 46 | `test_snapshot_requires_ingest_run` | `ingest_run_id` pointing at a nonexistent run → `IntegrityError`. Every price has provenance |
| 47 | `test_ingest_run_status_coherence` | `status='running'` with `finished_at` set, and `status='failed'` with null `error`, both rejected |
| 48 | `test_ingest_run_context_marks_failed` | An exception inside `ingest_run()` leaves `status='failed'` with a non-null `error` and a `finished_at` — the crash is recorded, not swallowed |

### `test_migrations.py` (Postgres)

| # | Test | Property asserted |
|---|---|---|
| 49 | `test_upgrade_downgrade_roundtrip` | `upgrade head` → `downgrade base` → `upgrade head` on a scratch DB, no error. Catches a broken `downgrade()` on day one instead of in six months |
| 50 | `test_models_match_migrations` | Alembic `compare_metadata` against `head` produces an **empty diff**. The single highest-value test here — it makes model/migration drift impossible to merge |

### `test_cli.py` (Postgres)

| # | Test | Property asserted |
|---|---|---|
| 51 | `test_log_bet_persists` | Every field round-trips; `status='open'`; `stake` "50.00" → `stake_cents == 5000` |
| 52 | `test_negative_odds_parsing` | `--odds=-150` parses to `-150`. Also assert the bare `--odds -150` form, since argparse's negative-number heuristic is fragile and this is exactly the flag where it bites |
| 53 | `test_stake_parsing_is_exact` | "50.10" → `5010` cents via `Decimal`, not `int(float("50.10")*100)` which yields `5009` |
| 54 | `test_settle_computes_payout` | Settle `won` on `$50 @ -150` → `payout_cents == 8333` (`5000 + round(5000 × 2/3)`, HALF_UP). `push` → `5000`. `lost` → `0` |
| 55 | `test_settle_payout_override` | `--payout` wins over the computed value, for when the book rounds differently |
| 56 | `test_rejects_moneyline_with_line` | Validation fires at the CLI boundary with a readable message, before the DB constraint |
| 57 | `test_clv_command_end_to_end` | `snapshot add` × 2 → `bet log` → `bet settle` → `bet clv` produces the expected `clv_pct`. The full loop in one test |
| 58 | `test_cli_opens_ingest_run` | Each mutating command leaves an `ingest_runs` row with `source='manual-cli'` and `status='success'` |

---

## 6. Decisions I'm uncertain about — your call

| # | Decision | Recommendation & tradeoff |
|---|---|---|
| **D1** | Money representation | **Integer cents (`BIGINT`).** Exact, no float money, matches the "integers at rest" philosophy of American odds. Alternative `NUMERIC(12,2)` reads better in raw SQL; "units" is what bettors think in but needs a unit-size table you don't have. Cents means every display path divides by 100 |
| **D2** | `market`/`selection`/`status` as `TEXT` + `CHECK` vs native PG `ENUM` | **TEXT + CHECK.** These will grow (props, F5, alternate lines). `ALTER TYPE ... ADD VALUE` is awkward inside Alembic and effectively irreversible on downgrade; dropping and re-adding a `CHECK` is a two-line migration. Cost: no type safety at the DB level, slightly larger rows |
| **D3** | Append-only idempotency | **Write unconditionally, no unique key, dedupe at read time.** A retried ingest run *can* therefore double-write. The alternative — a `UNIQUE` natural key with `ON CONFLICT DO NOTHING` — is retry-safe and is not technically an upsert (no UPDATE), but it sits close enough to your rule that I want you to rule on it rather than assume. Third option, write-only-on-change, is smallest but destroys "we checked at T and it hadn't moved," which is real information |
| **D4** | Postgres driver | **psycopg3 (`postgresql+psycopg://`).** One driver for both async (app) and sync (Alembic). `asyncpg` is measurably faster but is async-only, so Alembic needs a second driver and a second URL in config — two things to keep in sync for a workload that is not driver-bound |
| **D5** | `game_date NOT NULL` on `odds_snapshots` | **NOT NULL.** Forces the future odds worker to resolve the schedule before writing prices, which is the correct dependency order and structurally prevents v1's UTC-date bug. Cost: an odds poll cannot run standalone against an unknown `game_pk` |
| **D6** | Denormalized `commence_time_utc` on both tables vs a minimal `games` stub now | **Denormalize.** A `games` table is a Phase 1 concern and building a stub now means migrating it later. Append-only makes the denormalization honest — a postponement shows up as a new value on later rows, which is accurate history. Cost: the two tables can disagree, and there's no FK on `game_pk` |
| **D7** | Which book's close for CLV | **The same book you bet at**, for Phase 0. Pinnacle-close (or a consensus of sharp books) is the better long-run standard because a soft book's close is not an efficient price — but that needs multi-book ingest, which doesn't exist yet. The schema supports switching without a migration |
| **D8** | Does CLV require `status != 'open'`? | **No — compute whenever a closing snapshot exists.** You said "settled bet," and CLV is *reported* per settled bet, but it's *knowable* at first pitch and waiting for settlement only delays the signal. I'd add `--require-settled` as an opt-in flag. Say the word and I'll make settlement a hard precondition instead |
| **D9** | `line` as `NUMERIC(4,2)` | Covers quarter-lines (`7.75`) that some books post. `NUMERIC(4,1)` is enough for standard `.0`/`.5` totals and run lines. Cheap insurance; flagging only because it's easier to widen now than later |
| **D10** | Payout rounding | `Decimal` + `ROUND_HALF_UP`. Books vary and some truncate. The `--payout` override exists for disagreements; low stakes either way |

---

## 7. Deliberately NOT in this phase

**Data pipeline**
- No ingest workers. No Odds API client, no MLB StatsAPI client, no pybaseball.
  `ingest_runs` gets rows only from the CLI.
- No historical odds backfill.
- No line shopping or best-price-across-books logic. Multiple books are
  *storable* from day one; nothing chooses between them.

**Schema**
- No `games`, `teams`, `venues`, `players`, `predictions` tables. `game_pk` is a
  bare indexed integer with **no foreign key** — there's nothing to point at yet.
- No table partitioning on `odds_snapshots`, despite it being the one table that
  grows without bound. Revisit at ~10M rows; premature now.

**Application**
- No FastAPI app, no routers, no Pydantic API schemas, no auth.
- No React, no TypeScript, no frontend of any kind.
- No Docker/compose for the app — only a Postgres container for dev and tests.
- No CI (v1 has none either; worth adding, but not here).

**Modeling & betting logic**
- No models, no features, no training, no `simulate()`/`SimParams`. The
  `model_prob` column exists and nothing fills it.
- No Kelly sizing, bankroll tracking, ROI/P&L rollups, or CLV aggregation across
  bets. Per-bet CLV only.
- No parlays, teasers, or multi-leg. Single-leg bets only.
- No Shin's method. [claude.md](claude.md) permits it as an alternative; power
  method only in Phase 0. The `DevigResult` shape accommodates adding it later.
- Only three markets: `moneyline`, `total`, `run_line`. No player props, no
  alternate lines, no first-5-innings.

---

## 8. Build order

1. `pyproject.toml`, `.env.example`, `docker-compose.yml` (Postgres 16 only),
   `config.py`
2. `betting/odds.py` + `betting/devig.py` + their tests — **pure, no DB, and the
   part most likely to be wrong.** Get the hand-computed values passing before
   anything touches Postgres
3. `database/` — `base.py`, `utc.py`, `enums.py`, `engine.py`, `models.py`
4. Alembic init + revision `0001` + `test_migrations.py`
5. `test_schema.py` — constraints and the UTC/local-date pair
6. `betting/clv.py`: pure `compute_clv` first (+ `test_clv_math.py`), then the
   query functions (+ `test_clv_queries.py`)
7. `betting/settle.py`, `betting/cli.py`, `test_cli.py`

## 9. Verification

```bash
# Postgres up
docker compose up -d postgres

# Migrations apply, roll back, and reapply cleanly
alembic upgrade head && alembic downgrade base && alembic upgrade head

# Models match migrations — must print no diff
alembic check

# Full suite
pytest -v

# Pure-logic subset, no Docker needed
pytest tests/test_odds.py tests/test_devig.py tests/test_clv_math.py -v
```

End-to-end by hand, which is the real acceptance test for the phase:

```bash
# Two prices for the same selection, 3h apart
python -m betting.cli snapshot add --game-pk 776543 --game-date 2026-07-27 \
  --commence-time 2026-07-27T23:05:00Z --book pinnacle --market moneyline \
  --selection away --odds=+130 --captured-at 2026-07-27T20:05:00Z
python -m betting.cli snapshot add --game-pk 776543 --game-date 2026-07-27 \
  --commence-time 2026-07-27T23:05:00Z --book pinnacle --market moneyline \
  --selection away --odds=+110 --captured-at 2026-07-27T22:55:00Z
# The opposing side, so clv_prob_points is computable
python -m betting.cli snapshot add --game-pk 776543 --game-date 2026-07-27 \
  --commence-time 2026-07-27T23:05:00Z --book pinnacle --market moneyline \
  --selection home --odds=-130 --captured-at 2026-07-27T22:55:00Z

python -m betting.cli bet log --game-pk 776543 --game-date 2026-07-27 \
  --commence-time 2026-07-27T23:05:00Z --book pinnacle --market moneyline \
  --selection away --odds=+130 --stake 50.00
python -m betting.cli bet settle --id 1 --status won
python -m betting.cli bet clv --id 1
```

Expected: `clv_pct = +9.52%` (bet 2.30, closed 2.10), `beat_close = True`,
payout `$165.00`. Then confirm by hand in `psql`:

```sql
-- Three snapshot rows, none updated, all tied to a manual-cli run
SELECT id, selection, odds_american, captured_at, ingest_run_id FROM odds_snapshots;

-- The point-in-time query uses the index
EXPLAIN ANALYZE
SELECT odds_american FROM odds_snapshots
WHERE game_pk = 776543 AND market = 'moneyline' AND selection = 'away'
  AND book = 'pinnacle' AND line IS NULL
  AND captured_at < '2026-07-27T23:05:00Z'
ORDER BY captured_at DESC, id DESC LIMIT 1;
-- expect: Index Scan Backward using ix_odds_snapshots_pit, and NO Sort node.
-- Note this only means anything at scale - on a handful of rows Postgres
-- correctly picks whatever is cheapest. The automated test loads 8k rows and
-- ANALYZEs first.
```

---

## 10. What changed from the approved plan, and why

Three corrections, all found by building and measuring rather than reasoning.

### 10.1 A native Postgres 18 on port 5432 holds the v1 database

Not anticipated at all. `localhost:5432` on this machine is a native
PostgreSQL 18 install containing the v1 project's live data (18 tables:
`games`, `predictions`, `model_registry`, …, at Alembic revision
`c4d8e2b7a1f3`). The plan's default `DATABASE_URL` pointed straight at it, and
Alembic connected to it on the first run — `upgrade head` would have written
this project's schema onto real data, and the planned `downgrade base` test
would have dropped it.

Fixed by moving this project's container to **5433** (`docker-compose.yml`,
`config.py`, `.env.example`) and adding two guards in `tests/conftest.py`: the
test database name must end in `_test`, and the suite aborts if the target
contains any table this project does not own.

### 10.2 `line` in the point-in-time index was wrong

The plan's central performance claim — "O(log n) plus one tuple" — did not
hold for moneyline, the most common market. A btree only yields ordered output
on a trailing column when all preceding columns are equality-bound, and
`line IS NULL` is not an equality for pathkey purposes. Measured on 400k rows:

| Index | Moneyline plan | Est. cost |
|---|---|---|
| `(…, book, line, captured_at)` (planned) | Bitmap Index Scan + top-N Sort | 289.99 |
| `(…, book, captured_at, id)` (shipped) | Index Scan Backward, no Sort | 8.47 |

With `line = 8.5` (a real equality) the planned index was fine — the defect was
specific to NULL lines. Removing `line` makes it a cheap filter and restores
ordering for every market; a total that has moved across four lines still
resolves in under 0.1ms.

**The original test did not catch this**, because it asserted only that the
index name appeared in the plan — which it does under a bitmap scan too.
`TestIndexUsage` now asserts `Index Scan Backward` and the *absence* of `Sort`
and `Bitmap`, for both moneyline and totals, plus a test pinning the column
list so re-adding `line` fails loudly.

### 10.3 The two CLV metrics do not always agree in sign

The plan asserted they always agree and that disagreement meant bad data. That
is false, and a parametrised test found the counterexample: a bet of +200 on a
market closing +180/−220 is **+7.14% price CLV but −0.22 no-vig points**. You
beat the posted close by 20 cents, but the book's 4.5% overround is wider than
that, so against closing fair value the bet is still bad.

The real relationship is one-directional and provable: devigging lowers every
probability, so `clv_prob_points > 0` ⟹ `clv_pct > 0`, never the converse.
`clv_prob_points` is the stricter measure and the one to trust when they
disagree. Documented in `betting/clv.py` and asserted as an invariant.

### 10.4 Smaller deviations

- **Python 3.12 → 3.14.3.** 3.12 is not installed (3.13.7 and 3.14.3 are).
  `requires-python = ">=3.12"` is a floor; nothing uses a feature newer than
  `StrEnum`. Two 3.14 deprecations had to be routed around: the asyncio *policy*
  API (used `pytest_asyncio_loop_factories` instead) and
  `asyncio.iscoroutinefunction` (used `inspect`).
- **Windows needs `SelectorEventLoop`** for psycopg async — at runtime in
  `betting/cli.py`, not just under test.
- **`betting/odds.py` added** as a third module, so `clv.py` does not import
  conversion helpers from a devigging module.
- **`find_opposing_closing_snapshot` takes `before`** rather than deriving the
  cutoff from the closing row's own timestamp: a poll writes each side as its
  own row and the two can differ by milliseconds.
- **Migrations render `UtcDateTime` as `sa.DateTime(timezone=True)`** via a
  `render_item` hook, so no migration imports application code.
- The verification section's "payout $165.00" was arithmetic error: $50 at
  +130 returns **$115.00**.
