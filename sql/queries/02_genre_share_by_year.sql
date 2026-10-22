-- =============================================================================
-- Question: Which genres make up each release year's games - which genres
--           rise or fall over the years?  (M0 question.)
--
-- Result grain: one row per (release year, genre).
--
-- Why the aggregation is correct at this grain:
--   * A game can be listed under several genres, so app_genre is NOT one row
--     per game: joining it repeats a game once per genre. We never add rows up
--     across genres; within one (year, genre) a game appears at most once
--     (app_genre's primary key is app_key + genre_key), and COUNT(DISTINCT)
--     makes that explicit.
--   * The share divides by the number of games released that year, counted
--     in year_games BEFORE the genre join, so the denominator is not inflated
--     by multi-genre games. Shares within a year can therefore add up to more
--     than 100%.
--   * Only games with a full release date are included (release_date NOT NULL).
--
-- Note: covers the sampled apps that have a store page (app_detail). Small
-- years (1-3 games) give noisy shares. Runs on SQLite and MySQL 8.
-- =============================================================================

WITH dated AS (
    SELECT app_key,
           SUBSTR(release_date, 1, 4) AS release_year
    FROM app_detail
    WHERE app_type = 'game'
      AND release_date IS NOT NULL
),
year_games AS (
    SELECT release_year,
           COUNT(*) AS games
    FROM dated
    GROUP BY release_year
)
SELECT d.release_year,
       g.description                                            AS genre,
       COUNT(DISTINCT d.app_key)                                AS games_in_genre,
       y.games                                                  AS games_that_year,
       ROUND(100.0 * COUNT(DISTINCT d.app_key) / y.games, 1)    AS share_of_year_pct
FROM dated AS d
JOIN app_genre  AS ag ON ag.app_key     = d.app_key
JOIN genre      AS g  ON g.genre_key    = ag.genre_key
JOIN year_games AS y  ON y.release_year = d.release_year
GROUP BY d.release_year, g.genre_key, g.description, y.games
ORDER BY d.release_year, games_in_genre DESC, genre;
