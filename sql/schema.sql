-- =============================================================================
-- Steam catalogue database - schema (Milestone M2)
--
-- Safe to run more than once: every object is CREATE ... IF NOT EXISTS.
-- Every table is STRICT. Timestamps are TEXT in ISO 8601 UTC
-- (2026-10-03T11:38:18Z); dates are TEXT YYYY-MM-DD.
-- retrieved_at on every table = timestamp of the landed raw file the row
-- came from (taken from the file name written by src/ingest.py).
--
-- Entities  : app, genre, category, developer, publisher
-- 1:1 detail: app_detail        (store page of an app, latest version - type 1)
-- Bridges   : app_genre, app_category, app_developer, app_publisher
-- Events    : review_snapshot   (review counts of one app on one UTC day)
-- Quarantine: quarantine        (records the loader could not load)
-- =============================================================================

PRAGMA foreign_keys = ON;   -- per connection; src/db.py connect() also sets it


-- -----------------------------------------------------------------------------
-- app: one row = one Steam app in the catalogue (IStoreService/GetAppList).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app (
    app_key              INTEGER PRIMARY KEY,                       -- surrogate key
    steam_appid          INTEGER NOT NULL UNIQUE                    -- source identifier
                             CHECK (steam_appid > 0),
    name                 TEXT,                                      -- catalogue name (often the native-language title)
    last_modified_at     TEXT
                             CHECK (last_modified_at IS NULL OR last_modified_at GLOB
                                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    price_change_number  INTEGER
                             CHECK (price_change_number IS NULL OR price_change_number >= 0),
    retrieved_at         TEXT    NOT NULL
                             CHECK (retrieved_at GLOB
                                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;


-- -----------------------------------------------------------------------------
-- app_detail: one row = the latest retrieved store page of one app
-- (store.steampowered.com/api/appdetails, cc=us, l=english). Type 1: a newer
-- retrieval overwrites the older one. Only sampled apps have a row.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_detail (
    app_key                INTEGER PRIMARY KEY REFERENCES app (app_key),
    store_name             TEXT,                                    -- English store name
    app_type               TEXT,                                    -- 'game', 'dlc', ...
    is_free                INTEGER CHECK (is_free IN (0, 1)),
    required_age           INTEGER CHECK (required_age >= 0),
    release_date           TEXT                                     -- NULL when the source has no full date
                               CHECK (release_date IS NULL OR
                                      (release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                       AND date(release_date) = release_date)),
    release_date_text      TEXT,                                    -- release date exactly as the source wrote it
    coming_soon            INTEGER CHECK (coming_soon IN (0, 1)),
    price_currency         TEXT,
    price_initial_cents    INTEGER CHECK (price_initial_cents >= 0),
    price_final_cents      INTEGER CHECK (price_final_cents >= 0),
    discount_percent       INTEGER CHECK (discount_percent BETWEEN 0 AND 100),
    platform_windows       INTEGER CHECK (platform_windows IN (0, 1)),
    platform_mac           INTEGER CHECK (platform_mac IN (0, 1)),
    platform_linux         INTEGER CHECK (platform_linux IN (0, 1)),
    metacritic_score       INTEGER CHECK (metacritic_score BETWEEN 0 AND 100),
    recommendations_total  INTEGER CHECK (recommendations_total >= 0),
    retrieved_at           TEXT    NOT NULL
                               CHECK (retrieved_at GLOB
                                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;


-- -----------------------------------------------------------------------------
-- genre / category: one row = one Steam store genre / category.
-- Steam gives them numeric ids; the id is kept as the natural key.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS genre (
    genre_key       INTEGER PRIMARY KEY,
    steam_genre_id  INTEGER NOT NULL UNIQUE,
    description     TEXT,
    retrieved_at    TEXT    NOT NULL
                        CHECK (retrieved_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

CREATE TABLE IF NOT EXISTS category (
    category_key       INTEGER PRIMARY KEY,
    steam_category_id  INTEGER NOT NULL UNIQUE,
    description        TEXT,
    retrieved_at       TEXT    NOT NULL
                           CHECK (retrieved_at GLOB
                                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;


-- -----------------------------------------------------------------------------
-- developer / publisher: one row = one developer / publisher name.
-- Steam has no id for them, so the (trimmed) name is the source identifier.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS developer (
    developer_key  INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL UNIQUE CHECK (name <> ''),
    retrieved_at   TEXT    NOT NULL
                       CHECK (retrieved_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;

CREATE TABLE IF NOT EXISTS publisher (
    publisher_key  INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL UNIQUE CHECK (name <> ''),
    retrieved_at   TEXT    NOT NULL
                       CHECK (retrieved_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) STRICT;


-- -----------------------------------------------------------------------------
-- Bridge tables (many-to-many). One row = one app listed under one
-- genre / category / developer / publisher on its latest store page.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_genre (
    app_key       INTEGER NOT NULL REFERENCES app_detail (app_key),
    genre_key     INTEGER NOT NULL REFERENCES genre (genre_key),
    retrieved_at  TEXT    NOT NULL
                      CHECK (retrieved_at GLOB
                             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (app_key, genre_key)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS app_category (
    app_key       INTEGER NOT NULL REFERENCES app_detail (app_key),
    category_key  INTEGER NOT NULL REFERENCES category (category_key),
    retrieved_at  TEXT    NOT NULL
                      CHECK (retrieved_at GLOB
                             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (app_key, category_key)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS app_developer (
    app_key        INTEGER NOT NULL REFERENCES app_detail (app_key),
    developer_key  INTEGER NOT NULL REFERENCES developer (developer_key),
    retrieved_at   TEXT    NOT NULL
                       CHECK (retrieved_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (app_key, developer_key)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS app_publisher (
    app_key        INTEGER NOT NULL REFERENCES app_detail (app_key),
    publisher_key  INTEGER NOT NULL REFERENCES publisher (publisher_key),
    retrieved_at   TEXT    NOT NULL
                       CHECK (retrieved_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (app_key, publisher_key)
) STRICT, WITHOUT ROWID;


-- -----------------------------------------------------------------------------
-- review_snapshot (event): one row = review counts of one app on one UTC day.
-- If an app was fetched more than once on the same day, the most recently
-- retrieved response wins (decided by retrieved_at).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_snapshot (
    app_key            INTEGER NOT NULL REFERENCES app (app_key),
    snapshot_date      TEXT    NOT NULL
                           CHECK (snapshot_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                  AND date(snapshot_date) = snapshot_date),
    total_positive     INTEGER CHECK (total_positive >= 0),
    total_negative     INTEGER CHECK (total_negative >= 0),
    total_reviews      INTEGER CHECK (total_reviews >= 0),
    review_score       INTEGER CHECK (review_score BETWEEN 0 AND 9),
    review_score_desc  TEXT,
    retrieved_at       TEXT    NOT NULL
                           CHECK (retrieved_at GLOB
                                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (app_key, snapshot_date),
    CHECK (snapshot_date = substr(retrieved_at, 1, 10))
) STRICT;


-- -----------------------------------------------------------------------------
-- quarantine: one row = one landed record the loader could not load
-- (unknown app, no store data, unusable structure). Kept, never dropped.
-- A record quarantined as 'unknown_app' is removed from here once a later
-- app list contains that app (it is then loaded normally).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_key  INTEGER PRIMARY KEY,
    source          TEXT    NOT NULL CHECK (source IN ('applist', 'appdetails', 'appreviews')),
    source_file     TEXT    NOT NULL,          -- path under data/raw/
    record_ref      TEXT    NOT NULL,          -- which record in that file
    steam_appid     INTEGER,                   -- NULL if the record has no usable appid
    reason          TEXT    NOT NULL,
    detail          TEXT,
    retrieved_at    TEXT    NOT NULL
                        CHECK (retrieved_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    UNIQUE (source_file, record_ref)
) STRICT;


-- Indexes on foreign keys that are not the leading primary-key column
CREATE INDEX IF NOT EXISTS ix_app_genre_genre         ON app_genre (genre_key);
CREATE INDEX IF NOT EXISTS ix_app_category_category   ON app_category (category_key);
CREATE INDEX IF NOT EXISTS ix_app_developer_developer ON app_developer (developer_key);
CREATE INDEX IF NOT EXISTS ix_app_publisher_publisher ON app_publisher (publisher_key);
