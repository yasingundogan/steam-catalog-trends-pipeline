-- =============================================================================
-- Question: When in the calendar are the sampled games released - by year and
--           month, and by weekday?
--
-- Result grain: one row per release date that has at least one released game;
--               dates and months with no release are absent, so the caller must
--               show them as zero or as gaps.
-- Flag filter:  release_date_flag = 'ok' (a full date on or before retrieval);
--               no review flag (counts every released game).
-- Clock:        the store's calendar date (local to the store page, no time of
--               day): there is no hour to convert, so no time zone applies.
--               strftime('%w') gives 0 = Sunday .. 6 = Saturday.
-- =============================================================================

SELECT release_date,
       CAST(strftime('%Y', release_date) AS INTEGER) AS release_year,
       CAST(strftime('%m', release_date) AS INTEGER) AS release_month,
       CAST(strftime('%w', release_date) AS INTEGER) AS weekday,
       COUNT(*)                                      AS games
FROM app_detail_clean
WHERE app_type = 'game' AND coming_soon = 0 AND release_date_flag = 'ok'
GROUP BY release_date
ORDER BY release_date;
