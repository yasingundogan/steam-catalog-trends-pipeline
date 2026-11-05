-- =============================================================================
-- Steam catalogue database - cleaned layer (Milestone M3)
--
-- Built by src/clean.py from the M2 tables in sql/schema.sql. The M2 tables are
-- only READ: nothing here or in clean.py updates, deletes from or drops them.
--
-- Rules for every cleaned table
-- - Same grain, same primary key and the same number of rows as the M2 table
--   it cleans, with a foreign key to that row (flag, don't delete).
-- - For every value cleaning can change: <col>_raw (exactly as loaded in M2),
--   <col> (the cleaned value) and a flag saying why they differ. Where the
--   source delivers several values as one object (price_overview, the review
--   counts) one flag covers the group: see docs/cleaning_report.md section 5.
-- - Every flag is TEXT NOT NULL with a CHECK listing its allowed values.
-- - The cleaned value is never imputed: NULL when not trusted, standardised
--   when the text was inconsistent, otherwise unchanged.
--
-- Mapping tables hold every name, category and identity decision. They are
-- loaded from the committed CSV files in data/mappings/ on every run and keyed
-- by Steam's own ids or names, never by surrogate keys. Where the source id is
-- a UNIQUE column of an M2 entity table, the mapping table has a foreign key to
-- it, so a decision about an id that does not exist is an error, not a silent no-op.
--
-- Safe to run more than once: every object is CREATE ... IF NOT EXISTS.
-- Every table is STRICT.
-- =============================================================================

PRAGMA foreign_keys = ON;


-- =============================================================================
-- Mapping tables (one per CSV in data/mappings/)
-- =============================================================================

-- company_match.csv: one row = a person's decision about one pair of developer
-- / publisher names that the matching steps proposed as possibly the same company.
CREATE TABLE IF NOT EXISTS company_match (
    role_a      TEXT NOT NULL CHECK (role_a IN ('developer', 'publisher')),
    name_a      TEXT NOT NULL CHECK (name_a <> ''),
    role_b      TEXT NOT NULL CHECK (role_b IN ('developer', 'publisher')),
    name_b      TEXT NOT NULL CHECK (name_b <> ''),
    method      TEXT NOT NULL CHECK (method IN ('normalised_key', 'fuzzy', 'manual')),
    decision    TEXT NOT NULL CHECK (decision IN ('same', 'separate')),
    canonical   TEXT,                                     -- the name both map to when 'same'
    basis       TEXT NOT NULL CHECK (basis IN ('confirmed', 'inferred', 'unknown')),
    decided_by  TEXT NOT NULL CHECK (decided_by <> ''),
    decided_at  TEXT NOT NULL CHECK (date(decided_at) = decided_at),
    note        TEXT NOT NULL CHECK (note <> ''),
    PRIMARY KEY (role_a, name_a, role_b, name_b),
    CHECK ((decision = 'same' AND canonical IN (name_a, name_b))
        OR (decision = 'separate' AND canonical IS NULL))
) STRICT;

-- genre_kind.csv: one row = what one Steam genre id actually describes.
CREATE TABLE IF NOT EXISTS genre_kind (
    steam_genre_id  INTEGER PRIMARY KEY REFERENCES genre (steam_genre_id),
    description     TEXT NOT NULL,                        -- as seen when the decision was made
    kind            TEXT NOT NULL CHECK (kind IN ('genre', 'business_model', 'release_status')),
    decided_by      TEXT NOT NULL CHECK (decided_by <> ''),
    decided_at      TEXT NOT NULL CHECK (date(decided_at) = decided_at),
    note            TEXT NOT NULL CHECK (note <> '')
) STRICT;

-- category_label.csv: one row = a corrected label for one Steam category id.
CREATE TABLE IF NOT EXISTS category_label (
    steam_category_id  INTEGER PRIMARY KEY REFERENCES category (steam_category_id),
    description_raw    TEXT NOT NULL,                     -- the label Steam sent
    label              TEXT NOT NULL CHECK (label <> ''),
    reason             TEXT NOT NULL CHECK (reason IN ('placeholder_label', 'duplicate_label')),
    basis              TEXT NOT NULL CHECK (basis IN ('confirmed', 'inferred', 'unknown')),
    decided_by         TEXT NOT NULL CHECK (decided_by <> ''),
    decided_at         TEXT NOT NULL CHECK (date(decided_at) = decided_at),
    note               TEXT NOT NULL CHECK (note <> '')
) STRICT;

-- app_identity.csv: one row = a decision about a sampled app and another
-- catalogue app whose name has the same name key (possibly the same game).
CREATE TABLE IF NOT EXISTS app_identity (
    steam_appid    INTEGER NOT NULL REFERENCES app (steam_appid),
    related_appid  INTEGER NOT NULL REFERENCES app (steam_appid),
    method         TEXT NOT NULL CHECK (method IN ('name_key', 'manual')),
    decision       TEXT NOT NULL CHECK (decision IN ('same', 'separate')),
    basis          TEXT NOT NULL CHECK (basis IN ('confirmed', 'inferred', 'unknown')),
    decided_by     TEXT NOT NULL CHECK (decided_by <> ''),
    decided_at     TEXT NOT NULL CHECK (date(decided_at) = decided_at),
    note           TEXT NOT NULL CHECK (note <> ''),
    PRIMARY KEY (steam_appid, related_appid),
    CHECK (steam_appid <> related_appid)
) STRICT;


-- =============================================================================
-- Cleaned tables
-- =============================================================================

-- app_clean: one row = one row of app (one Steam app in the catalogue).
CREATE TABLE IF NOT EXISTS app_clean (
    app_key      INTEGER PRIMARY KEY REFERENCES app (app_key),
    steam_appid  INTEGER NOT NULL UNIQUE,
    name_raw     TEXT,
    name         TEXT,
    name_flag    TEXT NOT NULL CHECK (name_flag IN (
                     'ok', 'missing_not_provided', 'text_normalised')),
    name_key     TEXT                                     -- matching key (NFKC, Turkish fold, casefold, letters/digits only)
) STRICT;

-- app_detail_clean: one row = one row of app_detail (latest store page of one sampled app).
CREATE TABLE IF NOT EXISTS app_detail_clean (
    app_key                     INTEGER PRIMARY KEY,
    -- unchanged attributes, copied so M4 queries need only this table
    app_type                    TEXT,
    is_free                     INTEGER,
    coming_soon                 INTEGER,
    platform_windows            INTEGER,
    platform_mac                INTEGER,
    platform_linux              INTEGER,
    retrieved_at                TEXT NOT NULL,
    -- store name
    store_name_raw              TEXT,
    store_name                  TEXT,
    store_name_flag             TEXT NOT NULL CHECK (store_name_flag IN (
                                    'ok', 'missing_not_provided', 'text_normalised')),
    -- release date: _raw is the source text (app_detail.release_date_text)
    release_date_raw            TEXT,
    release_date                TEXT,                     -- YYYY-MM-DD, only when the day is known
    release_date_flag           TEXT NOT NULL CHECK (release_date_flag IN (
                                    'ok', 'announced_future', 'missing_not_reported',
                                    'missing_not_announced', 'partial_year', 'partial_quarter',
                                    'partial_month', 'invalid_future', 'invalid_implausible',
                                    'invalid_unparseable')),
    release_year                INTEGER,                  -- read from the text: full or partial date
    release_precision           TEXT NOT NULL CHECK (release_precision IN (
                                    'day', 'month', 'quarter', 'year', 'none')),
    -- price: one flag for the price_overview object (currency, initial, final, discount)
    price_currency_raw          TEXT,
    price_currency              TEXT,
    price_initial_cents_raw     INTEGER,
    price_initial_cents         INTEGER,
    price_final_cents_raw       INTEGER,
    price_final_cents           INTEGER,
    discount_percent_raw        INTEGER,
    discount_percent            INTEGER,
    price_flag                  TEXT NOT NULL CHECK (price_flag IN (
                                    'ok', 'missing_free', 'missing_unreleased', 'missing_not_reported',
                                    'invalid_currency', 'invalid_inconsistent', 'unusual_high')),
    -- optional store fields
    metacritic_score_raw        INTEGER,
    metacritic_score            INTEGER,
    metacritic_score_flag       TEXT NOT NULL CHECK (metacritic_score_flag IN (
                                    'ok', 'missing_not_reported')),
    recommendations_total_raw   INTEGER,
    recommendations_total       INTEGER,
    recommendations_total_flag  TEXT NOT NULL CHECK (recommendations_total_flag IN (
                                    'ok', 'missing_not_reported')),
    FOREIGN KEY (app_key) REFERENCES app (app_key),
    FOREIGN KEY (app_key) REFERENCES app_detail (app_key)
) STRICT;

-- review_snapshot_clean: one row = one row of review_snapshot (one app on one UTC day).
CREATE TABLE IF NOT EXISTS review_snapshot_clean (
    app_key                 INTEGER NOT NULL,
    snapshot_date           TEXT    NOT NULL,
    retrieved_at            TEXT    NOT NULL,
    -- review counts: one flag for the three counts of query_summary
    total_positive_raw      INTEGER,
    total_positive          INTEGER,
    total_negative_raw      INTEGER,
    total_negative          INTEGER,
    total_reviews_raw       INTEGER,
    total_reviews           INTEGER,
    review_counts_flag      TEXT NOT NULL CHECK (review_counts_flag IN (
                                'ok', 'zero_reviews', 'missing_not_reported',
                                'invalid_sum_mismatch', 'unusual_high')),
    -- review score and its label: one flag for the pair
    review_score_raw        INTEGER,
    review_score            INTEGER,
    review_score_desc_raw   TEXT,
    review_score_desc       TEXT,
    review_score_flag       TEXT NOT NULL CHECK (review_score_flag IN (
                                'ok', 'missing_no_reviews', 'missing_too_few_reviews',
                                'missing_not_reported', 'invalid_sentinel', 'invalid_inconsistent')),
    PRIMARY KEY (app_key, snapshot_date),
    FOREIGN KEY (app_key) REFERENCES app (app_key),
    FOREIGN KEY (app_key, snapshot_date) REFERENCES review_snapshot (app_key, snapshot_date)
) STRICT;

-- developer_clean / publisher_clean: one row = one developer / publisher name.
CREATE TABLE IF NOT EXISTS developer_clean (
    developer_key  INTEGER PRIMARY KEY REFERENCES developer (developer_key),
    name_raw       TEXT NOT NULL,
    name           TEXT NOT NULL,                         -- canonical company name
    name_flag      TEXT NOT NULL CHECK (name_flag IN ('ok', 'text_normalised', 'alias_mapped')),
    name_key       TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS publisher_clean (
    publisher_key  INTEGER PRIMARY KEY REFERENCES publisher (publisher_key),
    name_raw       TEXT NOT NULL,
    name           TEXT NOT NULL,
    name_flag      TEXT NOT NULL CHECK (name_flag IN ('ok', 'text_normalised', 'alias_mapped')),
    name_key       TEXT
) STRICT;

-- genre_clean / category_clean: one row = one Steam genre / category.
CREATE TABLE IF NOT EXISTS genre_clean (
    genre_key         INTEGER PRIMARY KEY REFERENCES genre (genre_key),
    steam_genre_id    INTEGER NOT NULL UNIQUE,
    description_raw   TEXT,
    description       TEXT,
    description_flag  TEXT NOT NULL CHECK (description_flag IN (
                          'ok', 'missing_not_reported', 'text_normalised')),
    kind              TEXT CHECK (kind IN ('genre', 'business_model', 'release_status')),
    kind_flag         TEXT NOT NULL CHECK (kind_flag IN ('mapped', 'unmapped'))
) STRICT;

CREATE TABLE IF NOT EXISTS category_clean (
    category_key       INTEGER PRIMARY KEY REFERENCES category (category_key),
    steam_category_id  INTEGER NOT NULL UNIQUE,
    description_raw    TEXT,
    description        TEXT,
    description_flag   TEXT NOT NULL CHECK (description_flag IN (
                           'ok', 'missing_not_reported', 'text_normalised',
                           'placeholder_label', 'duplicate_label'))
) STRICT;


-- =============================================================================
-- Views for M4: entity names joined in, identity resolved
-- =============================================================================

-- One row per sampled app: the app it resolves to. No app_identity decision is
-- 'same' yet, so every app resolves to itself; a 'same' decision would point
-- the newer appid at the older one.
CREATE VIEW IF NOT EXISTS app_resolved AS
SELECT a.app_key,
       a.steam_appid,
       COALESCE(MIN(CASE WHEN i.decision = 'same' THEN i.related_appid END), a.steam_appid) AS resolved_appid
FROM app_clean AS a
JOIN app_detail_clean AS d ON d.app_key = a.app_key
LEFT JOIN app_identity AS i ON i.steam_appid = a.steam_appid
GROUP BY a.app_key, a.steam_appid;

-- One row per sampled app with its latest review snapshot.
CREATE VIEW IF NOT EXISTS game_clean_v AS
WITH latest AS (
    SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r.app_key ORDER BY r.snapshot_date DESC) AS rn
    FROM review_snapshot_clean AS r
)
SELECT d.app_key,
       a.steam_appid,
       res.resolved_appid,
       d.store_name,
       d.app_type, d.is_free, d.coming_soon,
       d.release_date, d.release_year, d.release_precision, d.release_date_flag,
       d.price_initial_cents, d.price_final_cents, d.discount_percent, d.price_flag,
       d.platform_windows, d.platform_mac, d.platform_linux,
       l.snapshot_date,
       l.total_positive, l.total_negative, l.total_reviews, l.review_counts_flag,
       l.review_score, l.review_score_desc, l.review_score_flag
FROM app_detail_clean AS d
JOIN app_clean AS a ON a.app_key = d.app_key
JOIN app_resolved AS res ON res.app_key = d.app_key
LEFT JOIN latest AS l ON l.app_key = d.app_key AND l.rn = 1;

-- One row per app and genre, with the genre kind (M4 counts kind = 'genre' as genres).
CREATE VIEW IF NOT EXISTS app_genre_clean_v AS
SELECT ag.app_key, g.steam_genre_id, g.description AS genre, g.kind
FROM app_genre AS ag
JOIN genre_clean AS g ON g.genre_key = ag.genre_key;

-- One row per app and company role, with the canonical company name.
CREATE VIEW IF NOT EXISTS app_company_clean_v AS
SELECT ad.app_key, 'developer' AS role, dc.name AS company, dc.name_raw AS company_raw
FROM app_developer AS ad JOIN developer_clean AS dc ON dc.developer_key = ad.developer_key
UNION ALL
SELECT ap.app_key, 'publisher' AS role, pc.name AS company, pc.name_raw AS company_raw
FROM app_publisher AS ap JOIN publisher_clean AS pc ON pc.publisher_key = ap.publisher_key;
