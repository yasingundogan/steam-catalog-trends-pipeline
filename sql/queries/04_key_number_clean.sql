-- =============================================================================
-- Question: What share of its reviews is positive for the typical released game?
--           (M4 2.1 key number, computed on the cleaned layer.)
--
-- Result grain: one row (the whole sample of released games).
-- Flag filter:  review_score_flag IN :score_flags (reported: 'ok' = at least
--               10 reviews, Steam shows a score) AND review_counts_flag IN
--               :count_flags (reported: 'ok', 'unusual_high'). Both are JSON
--               arrays bound as parameters, never formatted into the SQL.
-- Clock:        latest snapshot per app (game_clean_v, snapshot_date UTC day).
-- =============================================================================

WITH games AS (
    SELECT 100.0 * total_positive / total_reviews AS share_pct,
           review_score
    FROM game_clean_v
    WHERE app_type = 'game' AND coming_soon = 0
      AND review_score_flag  IN (SELECT value FROM json_each(:score_flags))
      AND review_counts_flag IN (SELECT value FROM json_each(:count_flags))
),
ranked AS (
    SELECT share_pct,
           ROW_NUMBER() OVER (ORDER BY share_pct) AS rn,
           COUNT(*) OVER ()                       AS n
    FROM games
)
SELECT (SELECT COUNT(*) FROM game_clean_v
         WHERE app_type = 'game' AND coming_soon = 0)               AS released_games,
       (SELECT MAX(n) FROM ranked)                                  AS games_with_share,
       (SELECT ROUND(AVG(share_pct), 1) FROM ranked
         WHERE rn IN ((n + 1) / 2, (n + 2) / 2))                   AS median_positive_share_pct,
       (SELECT ROUND(AVG(share_pct), 1) FROM games)                 AS mean_positive_share_pct,
       (SELECT ROUND(AVG(review_score), 2) FROM games)              AS mean_review_score;
