-- =============================================================================
-- Question: How many games in our sample were released each year, what did
--           they cost, and how positive are their Steam reviews?
--           (M0 question: how the Steam catalogue changes by release year.)
--
-- Result grain: one row per release year (YYYY), plus one row 'no full date'
--               for games whose store page has no full release date
--               ('Coming soon', 'Q4 2026', ...; stored as NULL at load time).
--
-- Why the aggregation is correct at this grain:
--   * app_detail has exactly one row per app (primary key app_key), so
--     COUNT(*) per year counts every game once.
--   * review_snapshot has one row per app per UTC day. Joining it directly
--     would count an app once per day it was fetched, so latest_review keeps
--     only each app's most recent snapshot (rn = 1) BEFORE the join: the join
--     is then at most one-to-one and cannot multiply games.
--   * positive_share_pct = SUM(positive) / SUM(reviews) over the year: a
--     review-weighted share (games with many reviews weigh more). Games with
--     no reviews add 0 to both sums, so they do not distort it.
--   * avg_list_price_usd averages the list price (before discount) of paid
--     games only; free games and games with no price yet are NULL and AVG
--     skips them.
--
-- Note: covers the sampled apps that have a store page (app_detail), not the
-- whole 190k-app catalogue. Runs on SQLite (data/steam_catalog.db) and MySQL 8.
-- =============================================================================

WITH latest_review AS (
    SELECT app_key,
           total_positive,
           total_reviews,
           ROW_NUMBER() OVER (PARTITION BY app_key ORDER BY snapshot_date DESC) AS rn
    FROM review_snapshot
),
games AS (
    SELECT d.app_key,
           COALESCE(SUBSTR(d.release_date, 1, 4), 'no full date') AS release_year,
           d.coming_soon,
           d.is_free,
           d.price_initial_cents,
           r.total_positive,
           r.total_reviews
    FROM app_detail AS d
    LEFT JOIN latest_review AS r
           ON r.app_key = d.app_key
          AND r.rn = 1
    WHERE d.app_type = 'game'
)
SELECT release_year,
       COUNT(*)                                                         AS games,
       SUM(CASE WHEN coming_soon = 1 THEN 1 ELSE 0 END)                 AS not_yet_released,
       SUM(CASE WHEN is_free = 1 THEN 1 ELSE 0 END)                     AS free_games,
       ROUND(AVG(CASE WHEN is_free = 0 THEN price_initial_cents END) / 100.0, 2)
                                                                        AS avg_list_price_usd,
       SUM(CASE WHEN total_reviews > 0 THEN 1 ELSE 0 END)               AS games_with_reviews,
       SUM(total_reviews)                                               AS reviews,
       ROUND(100.0 * SUM(total_positive) / NULLIF(SUM(total_reviews), 0), 1)
                                                                        AS positive_share_pct
FROM games
GROUP BY release_year
ORDER BY release_year;
