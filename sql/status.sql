-- Operational health. `make status`.
--
-- Four questions, in the order they matter when something is wrong:
--   1. is ingest still running, and how stale is it
--   2. how much budget is left, and how fast is it going
--   3. are snapshots still arriving
--   4. are we capturing prices NEAR FIRST PITCH - the only ones that make
--      a closing line, and the thing a poll schedule can silently stop
--      doing while every other number still looks healthy

\echo '== last successful run per source =='
SELECT source,
       run_kind,
       max(started_at)                                   AS last_success,
       age(now(), max(started_at))                       AS staleness
  FROM ingest_runs
 WHERE status = 'success'
 GROUP BY source, run_kind
 ORDER BY last_success DESC;

\echo ''
\echo '== failed or stuck runs, last 7 days =='
SELECT id, source, run_kind, status, started_at,
       left(coalesce(error, ''), 80) AS error
  FROM ingest_runs
 WHERE started_at > now() - interval '7 days'
   AND status <> 'success'
 ORDER BY started_at DESC
 LIMIT 20;

\echo ''
\echo '== credits =='
-- `remaining_after` is what the API itself reported on the last poll, not
-- a number we computed - our arithmetic can drift, theirs cannot.
SELECT (params->>'remaining_after')::int AS remaining,
       (params->>'used_total')::int      AS used_this_period,
       started_at                        AS as_of
  FROM ingest_runs
 WHERE source = 'the-odds-api'
   AND params ? 'remaining_after'
 ORDER BY started_at DESC
 LIMIT 1;

SELECT date_trunc('day', started_at)::date AS day,
       sum(api_requests)                   AS credits_spent,
       count(*)                            AS polls
  FROM ingest_runs
 WHERE source = 'the-odds-api'
   AND started_at > now() - interval '14 days'
 GROUP BY 1 ORDER BY 1 DESC;

\echo ''
\echo '== snapshots per day, last 14 days =='
SELECT captured_at::date AS day,
       count(*)                  AS snapshots,
       count(DISTINCT game_pk)   AS games,
       count(DISTINCT book)      AS books
  FROM odds_snapshots
 WHERE captured_at > now() - interval '14 days'
 GROUP BY 1 ORDER BY 1 DESC;

\echo ''
\echo '== close proximity: games with a snapshot within 20 min of first pitch =='
-- THE number that says whether CLV means anything. A game can have
-- hundreds of snapshots and still no closing line if none of them landed
-- near first pitch.
WITH played AS (
    SELECT game_pk, game_date, commence_time_utc
      FROM games
     WHERE commence_time_utc < now()
       AND commence_time_utc > now() - interval '14 days'
), near_close AS (
    SELECT DISTINCT p.game_pk, p.game_date
      FROM played p
      JOIN odds_snapshots s ON s.game_pk = p.game_pk
     WHERE s.captured_at <= p.commence_time_utc
       AND s.captured_at >  p.commence_time_utc - interval '20 minutes'
)
SELECT p.game_date                                        AS day,
       count(*)                                           AS games,
       count(n.game_pk)                                   AS with_close,
       round(100.0 * count(n.game_pk) / nullif(count(*), 0), 1) AS pct
  FROM played p
  LEFT JOIN near_close n ON n.game_pk = p.game_pk
 GROUP BY 1 ORDER BY 1 DESC;

\echo ''
\echo '== unresolved events, last 14 days =='
-- The number that says whether team-name or start-time drift is quietly
-- eating games. An unresolved event writes no wrong price - it is skipped
-- and named - so nothing else in this report would show it. A rising count
-- means the crosswalk or the schedule window needs attention.
SELECT count(*) FILTER (WHERE params ? 'unresolved')          AS runs_with_unresolved,
       count(*)                                               AS runs_total,
       coalesce(sum(jsonb_array_length(params->'unresolved')), 0) AS events_skipped
  FROM ingest_runs
 WHERE started_at > now() - interval '14 days'
   AND run_kind IN ('odds_poll', 'odds_replay');

\echo ''
\echo '-- most recent unresolved examples --'
SELECT r.started_at, r.source, e AS unresolved_event
  FROM ingest_runs r
 CROSS JOIN LATERAL jsonb_array_elements_text(r.params->'unresolved') AS e
 WHERE r.params ? 'unresolved'
   AND r.started_at > now() - interval '14 days'
 ORDER BY r.started_at DESC
 LIMIT 10;
