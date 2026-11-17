-- =============================================================================
-- Question: One row per released game with what M4 compares: release date and
--           cohort inputs, price, genre membership and its positive share.
--
-- Result grain: one row per released sampled game (game_clean_v, coming_soon = 0).
-- Flag filter:  review_score_flag IN :score_flags AND review_counts_flag IN
--               :count_flags (JSON arrays bound as parameters). Pass every flag
--               value to see all released games.
-- Clock:        release_date is the store's calendar date (no time of day, no
--               time zone); snapshot_date is the UTC day of the latest snapshot;
--               age_days = snapshot_date - release_date.
-- Per-game data is the unit of analysis here (70 rows at most), so there is
-- nothing for SQL to aggregate further.
-- =============================================================================

SELECT v.app_key,
       v.steam_appid,
       v.store_name,
       v.release_date,
       v.release_year,
       CAST(julianday(v.snapshot_date) - julianday(v.release_date) AS INTEGER) AS age_days,
       v.is_free,
       v.price_initial_cents / 100.0                       AS price_usd,
       v.price_flag,
       v.total_positive,
       v.total_reviews,
       100.0 * v.total_positive / NULLIF(v.total_reviews, 0) AS share_pct,
       v.review_score,
       v.review_counts_flag,
       v.review_score_flag,
       EXISTS (SELECT 1 FROM app_genre_clean_v AS g
               WHERE g.app_key = v.app_key AND g.kind = 'genre' AND g.genre = 'Indie') AS is_indie
FROM game_clean_v AS v
WHERE v.app_type = 'game' AND v.coming_soon = 0
  AND v.review_score_flag  IN (SELECT value FROM json_each(:score_flags))
  AND v.review_counts_flag IN (SELECT value FROM json_each(:count_flags))
ORDER BY v.release_date, v.app_key;
