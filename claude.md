# baseball-predictor

## Non-negotiable rules

### Point-in-time correctness
Any feature for a game on date D may ONLY use data from games strictly before D.
- Rolling aggregations MUST be followed by .shift(1) within the player group.
- Never join season-level aggregates onto individual games.
- Every feature row carries `computed_at` and `as_of_date`.
- Before writing feature code, re-read this section.

### Devigging
Use the power method (solve for k where sum(p_i^k) = 1) or Shin's method.
NEVER use multiplicative normalization (p_i / sum(p)) — it biases favorites.

### Simulation
`simulate(params: SimParams) -> np.ndarray` is a PURE function.
No DB access, no network, no config reads. Backtest and live API call the
identical function. If you're tempted to add a db_session arg, stop.

## Stack
Python 3.12, FastAPI, Postgres 16, SQLAlchemy 2.0 (async), pandas, numpy,
pytest. Frontend: React + TypeScript + Tailwind + Vite.

## Conventions
- Money/odds stored as American integers, converted at the edges only.
- All timestamps UTC in DB. `game_date` is the LOCAL date at the venue.
- Games keyed by `game_pk` (int), never by (date, teams) — doubleheaders exist.

## Before writing code against pybaseball or statsapi
Read the actual installed package source or hit the endpoint and inspect the
response. Do not write code from memory of these APIs.