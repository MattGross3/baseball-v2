# Known gaps

Things that are wrong, incomplete, or deferred *on purpose*. Written down so
they get rediscovered as decisions rather than as bugs.

Distinct from "not built yet" (see the plan's out-of-scope list). Everything
here is a place where what exists is knowingly imperfect.

---

## A postponement leaves a bet's CLV cutoff stale

**Phase 0. Not fixable here.**

`bets.commence_time_utc` is denormalised and frozen at the moment the bet is
logged. It is the cutoff that defines "closing line": the last snapshot
strictly before it.

If a game is postponed and rescheduled, later `odds_snapshots` rows carry the
corrected `commence_time_utc` (they record whatever the source reported at
capture time, which is the honest thing for an append-only table to do). The
bet does not. Its CLV would then be measured against a cutoff that no longer
corresponds to first pitch — most likely returning a price from hours before
the game actually started, or none at all.

Not fixable in Phase 0 because there is no ingest worker to notice the
correction, and no `games` table to hold the authoritative time. The fix
belongs with the schedule ingest: when a game's start time changes, update
the open bets that reference it.

Impact today: nil. Nothing is polled automatically, and hand-entered bets are
entered after the fact.

---

## `game_pk` has no foreign key

**Deliberate.** There is no `games` table to point at. `game_pk` is a bare
indexed integer with a `> 0` check.

Consequence: nothing stops a typo'd `game_pk` from being logged, and
`odds_snapshots` and `bets` can disagree about a game's `game_date` or
`commence_time_utc`. Both are caught in practice by the CLV lookup returning
nothing, which is a poor error message.

Closes when the schedule ingest lands and `games` becomes the owner.

---

## `game_date` is supplied by the caller, not derived

**Deliberate, and the alternative is a bug.**

`game_date` must be the venue-local date, which is StatsAPI's `officialDate`.
It cannot be computed from `commence_time_utc` without the venue's timezone,
and computing it as `commence_time_utc::date` is precisely the defect that
loses every late West Coast game.

So the CLI requires `--game-date`, and the future odds worker will have to
resolve the schedule before it can write prices. That ordering constraint is
intended, not incidental.

---

## No stored CLV columns

`bets` has no `closing_odds_american` or `clv_prob_points`. CLV is computed
on demand from snapshots.

This is right while the method is still moving — materialising a number
produced by a formula that might change is how you end up with a CLV series
you cannot trust or explain. It will stop being right in Phase 4, when
`avg(clv_prob_points)` over a season means recomputing a devig per row.

Revisit then, and when doing so, store the inputs (`closing_odds_american`,
`opposing_closing_odds_american`, `closing_book`) alongside the outputs so a
recomputation is possible.

---

## `backend_pid` is captured on the wrong connection

**Known and deliberately not fixed yet. The fix is one line; the risk is one
wrong status on an audit row.**

`ingest_run()` reads `pg_backend_pid()` *before* its first commit. Committing
returns the connection to the pool, so the pid recorded belongs to the setup
connection rather than the one that goes on to do the work.

Under `QueuePool` the same connection usually comes back, which is why the
tests pass — but that is luck, not a guarantee. `PID_REAP_GRACE` covers
start-up churn only; it does nothing for a long run that gets handed a
different connection. The Phase 2 Statcast backfill is exactly that shape: a
run long enough for the pool to have moved on, and long enough for the reaper
to look at it.

Symptom if it bites: a live run marked `failed` with `reaped: backend gone`.
It cannot corrupt a price or a bet — `ingest_runs` is an audit table, and the
work's own transaction is unaffected — which is why this is recorded rather
than rushed.

The fix, when it matters:

```python
# in ingest_run(), after the run row and the reap have both committed
run.backend_pid = await _backend_pid(session)
await session.commit()
```

Re-reading after the commits binds the pid to the connection the work will
actually run on. Do it then rather than now, so the change lands with a test
that holds a connection across a commit and asserts the pid still matches.

---

## The reaper judges liveness by pid, which is single-node only

`reap_stale_runs` asks `pg_stat_activity` whether the backend that opened a
run still exists. That is evidence, where a wall-clock timeout is only
inference — but it rests on two assumptions, both violable:

- **A run holds its connection for its duration.** True under
  `session_scope()`. A future writer that commits, releases the connection,
  and later picks up a different one could have its recorded pid go stale
  while it is alive. `PID_REAP_GRACE` (5 min) is the guard: no run is reaped
  on pid evidence until at least that old, so ordinary pool churn cannot
  produce a false positive.

- **Pids are per-server.** A run started against this database from a
  *different host* has a pid meaningful only on that host, and the query
  would judge it against the wrong machine's process table — potentially
  marking a healthy run failed. Correct for single-node, wrong the moment
  there are two.

When a second host appears, either add a host column and compare on
`(host, pid)`, or switch to a Postgres advisory lock held for the run's
duration, which is host-agnostic and releases automatically on disconnect.

The age-based path (`STALE_RUN_AFTER`, 1 hour) remains as a fallback for
rows with no `backend_pid`.

`_is_downgrade()` in `migrations/env.py` also reads a private Alembic
attribute (`_migrations_fn.__name__`) because there is no public accessor for
migration direction. It can break on an Alembic upgrade; the downgrade-guard
tests are what would catch it.

---

## CLV resolves against the book you bet at

**Deliberate for Phase 0, wrong long-term.**

A soft book's closing line is not an efficient price. The better yardstick is
a sharp reference — Pinnacle, or a consensus — but that needs multi-book
ingest, which does not exist.

`compute_clv_for_bet(..., reference_book=...)` already takes the parameter,
so the switch is a changed default rather than a refactor. The schema stores
per-book prices from day one.

---

## `odds_snapshots` is unpartitioned

It is the one table that grows without bound and is never deleted from.

At Phase 0 volumes this is irrelevant. Revisit around 10M rows, when
`captured_at` range partitioning becomes worth the complexity. The
point-in-time indexes are prefixed by `game_pk`, so partitioning by time
would need thought about whether it helps or hurts them.

---

## The poll budget will bias CLV unless the schedule is weighted

**Decided: weight the schedule toward first pitch. Phase 1 scheduler design
constraint, not a later optimisation.**

The Odds API free tier is 500 credits/month account-wide, and **a credit is
charged per market per region, not per request** — measured, not assumed:
a single-market call returned `x-requests-last: 1`.

**Settled: h2h + totals, US region = 2 credits a poll.** That is ~250 polls a
month, or **~8 a day**. One request still returns every game in the slate, so
cost scales with markets, not with games.

**Spreads is ruled out.** Adding it is a 50% cost increase (2 → 3 credits,
~8/day → ~5.5/day) for a market nothing currently measures. Revisit only if
run-line CLV becomes a question worth answering.

The problem is what CLV means. A closing line is the price near first pitch,
and MLB start times spread across roughly six hours. Sixteen evenly-spaced
polls put ~90 minutes between them, so the last snapshot before a given game
could be an hour and a half stale — and the final 30 minutes is exactly when
lineup confirmations and steam land. A CLV series measured that way is not
measuring the close; it is measuring a T−90min proxy and calling it the close.
Every number this phase exists to produce would carry that bias, invisibly.

At ~8 polls a day, clustering is not an optimisation — it is the only way
the number means anything. MLB start times bunch into a handful of clusters
(roughly 13:05, 19:05/19:10, 20:10, 22:10 ET). Spending nearly the whole
daily budget just before those clusters, and accepting almost no early
coverage, is the trade: line-movement history is nice to have, a close is
the thing being measured.

One lever if ~8/day proves too tight: **drop to h2h only**, 1 credit a poll,
~16 polls a day. Moneyline CLV is the metric Phase 0 actually measures; totals
are collected because they are cheap alongside it, not because anything reads
them yet. It is a one-line change to `MARKETS` in ingestion/odds.py.

Two consequences to design in from the start, not bolt on:

- The scheduler must read the day's start times and derive its poll times
  from them, rather than running on a fixed cron. A fixed cron cannot know
  that today's slate is all afternoon games.
- `ingest_runs.api_requests` already exists to make budget consumption
  reconstructible from history. It needs to be populated from day one or the
  budget is being managed blind.

Until that scheduler exists, any CLV computed from automatically-polled data
should be read as approximate. CLV from hand-entered prices (the current
`snapshot add` path) is exact, because the timestamps are whatever was
actually observed.

---

## Our close is a SOFT close: six books, no Pinnacle

The books returned for `regions=us`, observed on a live h2h+totals capture,
2026-07-28:

    betmgm, betonlineag, betrivers, betus, bovada,
    draftkings, fanduel, lowvig, mybookieag

**Nine, not six.** An earlier h2h-only sample returned six; the fuller
request returns nine, so book coverage varies with the markets requested and
six is not the ceiling. Coverage also varies per event - a game had prices
from 2 books at one point and 9 at another - so "best price across books"
means best of whoever quoted that game at that instant, not a fixed panel.

**No Pinnacle, and no Circa.** Those are the sharp books whose closing line is
the usual yardstick for "the market's honest final opinion". What we can
measure is a soft close, and best-price-across-books means best of these six.

Consequences, all of which bias the number in the same direction:

- A soft book's close is less efficient than a sharp one, so beating it is
  easier. CLV measured this way will read better than the same bets would
  against Pinnacle.
- Soft books move on their own customers, not only on information, so some of
  the movement being measured is their book management rather than a
  consensus converging.
- `compute_clv_for_bet(reference_book=...)` already takes a parameter, so
  switching yardsticks later is a changed default, not a refactor. But the
  data has to exist first, and it does not.

Not fixable inside this budget. Pinnacle is available on The Odds API's
`eu`/`au` regions, which would cost an extra credit per market per region -
doubling the poll cost to reach one book. Recorded rather than acted on.

Whether nine is tier-limited could not be determined: the response carries
no indication of what a higher tier would add, and the documentation does not
enumerate per-tier coverage. What IS established is that the count varies
with the markets requested and per event, so treat nine as an observation
rather than a fixed roster.

---

## StatsAPI serves the droplet only a narrow current window

**Operational, discovered in production, works around itself.**

MLB StatsAPI gates requests by source address. Measured from the droplet
(DigitalOcean, 104.248.124.37) versus a residential connection:

| request | droplet | residential |
|---|---|---|
| `date=<today±1>` | 200 | 200 |
| `date=<any historical date>` | **406** | 200 |
| `startDate=..&endDate=..` | **406** | 200 |
| `date=..&gameType=R` | **406** | 200 |
| `date=..&hydrate=team,venue` | **406** | 200 |

Not rate limiting: it is stable across pauses and reproducible per-parameter.
The 406 body is a bare Spring error with no explanation.

Consequences, both already handled:

- `fetch_schedule` issues **one request per day with only `sportId` and
  `date`**, and filters `gameType` client-side. Nothing is lost - the base
  payload already carries every field `game_values()` reads.
- **The season backfill cannot run on the droplet.** It runs from a
  residential connection, writing into the droplet's Postgres over an SSH
  tunnel (`ssh -N -L 15433:localhost:5433 baseball`) - which is possible
  precisely because Postgres is bound to loopback rather than exposed.

The daily cron schedule job only needs today ±1, so it is unaffected. But if
a future job needs historical schedule data on the box, it will hit this,
and the answer is the tunnel rather than a workaround in the client.

---

## A twice-postponed game loses its intermediate history

`games.rescheduled_from_date` holds one date. A game postponed twice keeps
only the most recent origin; the middle hop is lost.

About 2% of games are postponed, so a double postponement is rare enough to
accept. Deliberately not built for. If it ever matters, the fix is a
separate `game_reschedules` table, not a second column.

There is no `rescheduled_as` column, and that is not an omission: StatsAPI
REUSES the gamePk across a postponement (pk 778431 was postponed 2025-04-06
and played 2025-08-09 under the same pk), so there is no successor game to
point at. A permanently-NULL column would be a claim about the data that is
not true, and the next reader would assume something populates it.

The market-episode distinction is already carried without extra structure:
that game accumulates snapshots under one pk across two episodes, but each
snapshot stores `game_date` as reported at capture, so April rows say
2025-04-06 and August rows say 2025-08-09. Separable by a plain GROUP BY.
That is decision D6 (denormalise rather than normalise) paying off in a way
that was not anticipated when it was made — so do not "simplify" the
denormalised copy away later.

---

## OPEN: is The Odds API's player-props endpoint per-event?

**Confirm before writing any Phase 4 prop code.**

If props are quoted per-event rather than per-slate, then one request per
game per poll — ~15 games — consumes the entire monthly budget in about two
days. That makes props a pricing decision (paid tier, or a different
provider) rather than an engineering one, and it should be settled before
code exists that assumes otherwise.

Do not infer the answer from the moneyline endpoint's behaviour; check the
documentation and a real response.

---

## v1 must be ported or killed at the end of Phase 2

**The strategic one.**

There are currently two systems. `AI-Testing/baseball-predictor` has ~9,270
lines of Python, 16 tables, ten migrations, and real ingested data. This
repository has a cleaner schema and better invariants, and no data.

"Harvest v1 later" is not a plan, it is how both systems stay half-alive. The
specific failure mode: models keep running in v1 because that is where the
data is, bets keep being logged in v2 because that is where CLV is, and the
model probabilities and the CLV series end up in different databases with
nothing joining them.

**Decision point: end of Phase 2.** By then this repository has ingest and a
`games` table, which is the point at which porting the feature and model
layers is mechanical. Either that port happens, or v2 is abandoned and its
corrections are back-ported into v1. Not both.

What v1 has that is worth taking: the feature builders (`features/`, eight
modules, all as-of-date bounded), the walk-forward splitters in
`models/model_utils.py`, the training-median imputation in `predict.py`, and
the park-factor reference data.

What must not come across: `backtest/clv_tracker.py` (multiplicative devig),
the `(date, home_name, away_name)` game match in `ingestion/odds_api.py`, and
the 19 uses of `dt.date.today()`.
