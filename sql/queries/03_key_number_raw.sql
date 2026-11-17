-- =============================================================================
-- Question: What share of its reviews is positive for the typical released game?
--           (M4 2.1 key number, computed on the M2 tables exactly as loaded.)
--
-- Result grain: one row (the whole sample of released games).
-- Flag filter:  none - the M2 tables have no flags. Every released game with a
--               latest snapshot counts; review_score = 0 is averaged as a number.
-- Clock:        latest snapshot per app (snapshot_date is a UTC day).
--
-- positive share = total_positive / total_reviews per game; SQLite returns NULL
-- for 0 / 0, so games without reviews drop out of the share silently. The median
-- is the middle row (or the mean of the two middle rows) of the ordered shares.
-- =============================================================================

WITH latest AS (
    SELECT app_key, total_positive, total_reviews, review_score,
           ROW_NUMBER() OVER (PARTITION BY app_key ORDER BY snapshot_date DESC) AS rn
    FROM review_snapshot
),
games AS (
    SELECT 100.0 * l.total_positive / NULLIF(l.total_reviews, 0) AS share_pct,
           l.review_score
    FROM app_detail AS d
    JOIN latest AS l ON l.app_key = d.app_key AND l.rn = 1
    WHERE d.app_type = 'game' AND d.coming_soon = 0
),
ranked AS (
    SELECT share_pct,
           ROW_NUMBER() OVER (ORDER BY share_pct) AS rn,
           COUNT(*) OVER ()                       AS n
    FROM games
    WHERE share_pct IS NOT NULL
)
SELECT (SELECT COUNT(*) FROM games)                                 AS released_games,
       (SELECT MAX(n) FROM ranked)                                  AS games_with_share,
       (SELECT ROUND(AVG(share_pct), 1) FROM ranked
         WHERE rn IN ((n + 1) / 2, (n + 2) / 2))                   AS median_positive_share_pct,
       (SELECT ROUND(AVG(share_pct), 1) FROM games)                 AS mean_positive_share_pct,
       (SELECT ROUND(AVG(review_score), 2) FROM games)              AS mean_review_score;
