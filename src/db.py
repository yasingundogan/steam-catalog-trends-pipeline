"""Shared database helpers.

Every script (load.py, checks.py, ...) opens the database through connect(),
which turns foreign-key enforcement on immediately after connecting.

SQL in this file is static text only: no value - and no table name - is ever
formatted into a statement. Values always go through ? placeholders.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "steam_catalog.db"
RAW_DIR = REPO_ROOT / "data" / "raw"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

MIN_SQLITE = (3, 37, 0)  # first version with STRICT tables


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open the database and enforce foreign keys on this connection."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old: STRICT tables need "
            f"{'.'.join(map(str, MIN_SQLITE))} or newer. Upgrade Python (3.11+) or Anaconda."
        )
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        con.close()
        raise RuntimeError("could not enable PRAGMA foreign_keys on this connection")
    return con


def apply_schema(con: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    """Run sql/schema.sql (safe to run any number of times)."""
    con.executescript(schema_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- table helpers
# One static statement per table, so counting / dumping never formats a table
# name into SQL. If schema.sql gains a table, add it here: known_tables() fails
# loudly until you do, so no table can silently escape the checks.
COUNT_SQL = {
    "app":             "SELECT COUNT(*) FROM app",
    "app_detail":      "SELECT COUNT(*) FROM app_detail",
    "genre":           "SELECT COUNT(*) FROM genre",
    "category":        "SELECT COUNT(*) FROM category",
    "developer":       "SELECT COUNT(*) FROM developer",
    "publisher":       "SELECT COUNT(*) FROM publisher",
    "app_genre":       "SELECT COUNT(*) FROM app_genre",
    "app_category":    "SELECT COUNT(*) FROM app_category",
    "app_developer":   "SELECT COUNT(*) FROM app_developer",
    "app_publisher":   "SELECT COUNT(*) FROM app_publisher",
    "review_snapshot": "SELECT COUNT(*) FROM review_snapshot",
    "quarantine":      "SELECT COUNT(*) FROM quarantine",
}

DUMP_SQL = {
    "app":             "SELECT * FROM app ORDER BY app_key",
    "app_detail":      "SELECT * FROM app_detail ORDER BY app_key",
    "genre":           "SELECT * FROM genre ORDER BY genre_key",
    "category":        "SELECT * FROM category ORDER BY category_key",
    "developer":       "SELECT * FROM developer ORDER BY developer_key",
    "publisher":       "SELECT * FROM publisher ORDER BY publisher_key",
    "app_genre":       "SELECT * FROM app_genre ORDER BY app_key, genre_key",
    "app_category":    "SELECT * FROM app_category ORDER BY app_key, category_key",
    "app_developer":   "SELECT * FROM app_developer ORDER BY app_key, developer_key",
    "app_publisher":   "SELECT * FROM app_publisher ORDER BY app_key, publisher_key",
    "review_snapshot": "SELECT * FROM review_snapshot ORDER BY app_key, snapshot_date",
    "quarantine":      "SELECT * FROM quarantine ORDER BY quarantine_key",
}

TABLES = tuple(COUNT_SQL)


def known_tables(con: sqlite3.Connection) -> tuple:
    """Tables in the database; raises if one is missing from COUNT_SQL/DUMP_SQL."""
    names = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    unknown = names - set(TABLES)
    if unknown:
        raise RuntimeError(f"tables not registered in db.py COUNT_SQL/DUMP_SQL: {sorted(unknown)}")
    return tuple(t for t in TABLES if t in names)


def row_counts(con: sqlite3.Connection) -> dict:
    """{table: row count} for every table in the database."""
    return {t: con.execute(COUNT_SQL[t]).fetchone()[0] for t in known_tables(con)}


def table_fingerprints(con: sqlite3.Connection) -> dict:
    """{table: sha256 of its full contents} - equal fingerprints mean identical tables."""
    result = {}
    for t in known_tables(con):
        digest = hashlib.sha256()
        for row in con.execute(DUMP_SQL[t]):
            digest.update(repr(row).encode("utf-8"))
            digest.update(b"\n")
        result[t] = digest.hexdigest()
    return result
