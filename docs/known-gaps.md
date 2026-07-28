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

The Odds API free tier is 500 requests/month account-wide: ~16 per day. That
is affordable at slate level, because one request returns every game for a
sport/region/market set. It is *not* affordable naively.

The problem is what CLV means. A closing line is the price near first pitch,
and MLB start times spread across roughly six hours. Sixteen evenly-spaced
polls put ~90 minutes between them, so the last snapshot before a given game
could be an hour and a half stale — and the final 30 minutes is exactly when
lineup confirmations and steam land. A CLV series measured that way is not
measuring the close; it is measuring a T−90min proxy and calling it the close.
Every number this phase exists to produce would carry that bias, invisibly.

So the poller must cluster requests around start times rather than spreading
them evenly. MLB start times bunch into a handful of clusters per day
(roughly 13:05, 19:05/19:10, 20:10, 22:10 ET), so a workable allocation is
about half the daily budget fired just before those clusters and the rest
spread thinly for line-movement history. Coarse early coverage is an
acceptable loss; a stale close is not.

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
