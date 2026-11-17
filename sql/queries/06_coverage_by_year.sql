-- =============================================================================
-- Question: For each release year, how many sampled games could have a positive
--           share, and how many actually have one we trust?
--
-- Result grain: one row per release year (release_year from the store text,
--               full or partial date) plus one row per game with no year (NULL).
-- Flag filter:  none - this table counts every flag value so the denominators
--               are visible: usable = review_score_flag 'ok' (>= 10 reviews).
-- Clock:        release_year is the store's calendar year; snapshot_date UTC.
-- =============================================================================

SELECT release_year,
       COUNT(*)                                                         AS sampled_games,
       SUM(coming_soon = 0)                                             AS released_games,
       SUM(coming_soon = 0 AND total_reviews > 0)                       AS with_any_review,
       SUM(coming_soon = 0 AND review_score_flag = 'ok')                AS usable_score,
       SUM(coming_soon = 0 AND review_score_flag = 'missing_too_few_reviews') AS too_few_reviews,
       SUM(coming_soon = 0 AND review_score_flag = 'missing_no_reviews')      AS no_reviews,
       SUM(coming_soon = 1)                                             AS not_released
FROM game_clean_v
WHERE app_type = 'game'
GROUP BY release_year
ORDER BY release_year IS NULL, release_year;
