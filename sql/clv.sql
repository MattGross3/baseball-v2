-- Closing line value over settled bets. `make clv`.
--
-- Reports both metrics because they answer different questions and do not
-- always agree in sign: clv_pct is whether you beat the posted close,
-- clv_prob_points is whether you beat its devigged fair value. The second
-- is stricter - see betting/clv.py.
--
-- Deliberately recomputed from snapshots rather than read from a stored
-- column. The devigging method is still settling, and a materialised CLV
-- series computed by a formula that later changed is one nobody can trust
-- or explain. See docs/known-gaps.md.

\echo '== settled bets with a resolvable close =='
\echo 'NOTE: this reports coverage only. The CLV numbers themselves come from'
\echo 'betting/clv.py, which does the power-method devig that SQL should not.'
\echo ''

WITH settled AS (
    SELECT b.id, b.game_pk, b.book, b.market, b.selection, b.line,
           b.odds_american, b.stake_cents, b.payout_cents, b.status,
           b.commence_time_utc
      FROM bets b
     WHERE b.status <> 'open'
), closing AS (
    SELECT s.id AS bet_id,
           (SELECT o.odds_american
              FROM odds_snapshots o
             WHERE o.game_pk   = s.game_pk
               AND o.market    = s.market
               AND o.selection = s.selection
               AND o.book      = s.book
               AND (o.line IS NOT DISTINCT FROM s.line)
               AND o.captured_at < s.commence_time_utc
             ORDER BY o.captured_at DESC, o.id DESC
             LIMIT 1) AS closing_odds
      FROM settled s
)
SELECT count(*)                                            AS settled_bets,
       count(c.closing_odds)                               AS with_close,
       round(100.0 * count(c.closing_odds)
             / nullif(count(*), 0), 1)                     AS pct_with_close
  FROM settled s
  LEFT JOIN closing c ON c.bet_id = s.id;

\echo ''
\echo '== realised P&L (settlement, not CLV) =='
SELECT count(*)                                       AS bets,
       sum(stake_cents)  / 100.0                      AS staked,
       sum(payout_cents) / 100.0                      AS returned,
       (sum(payout_cents) - sum(stake_cents)) / 100.0  AS profit,
       round(100.0 * (sum(payout_cents) - sum(stake_cents))
             / nullif(sum(stake_cents), 0), 2)        AS roi_pct
  FROM bets
 WHERE status <> 'open';

\echo ''
\echo '== per-bet CLV: run `python -m betting.cli bet clv --id <n>` =='
SELECT id, game_pk, book, market, selection, odds_american, status
  FROM bets
 WHERE status <> 'open'
 ORDER BY id DESC
 LIMIT 20;
