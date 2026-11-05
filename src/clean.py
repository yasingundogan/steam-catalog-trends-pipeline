"""Build the cleaned layer (sql/clean_schema.sql) from the M2 tables.

Run from the repository root, after python src/load.py:

    python src/clean.py              # builds / rebuilds the cleaned layer in data/steam_catalog.db
    python src/clean.py --db x.db    # another database

Pipeline: raw files -> M2 tables (src/load.py) -> cleaned layer (this script).
This script never reads data/raw/ and never calls the API.

How it behaves
- Raw untouched: the M2 tables are only read (SELECT). Every write statement in
  this file is checked at import time to target a cleaned or mapping table,
  and every run fingerprints the M2 tables before and after and fails if they differ.
- Flag, don't delete: every cleaned table has the grain, primary key and row
  count of the M2 table it cleans. A value we do not trust becomes NULL with a
  flag saying why; nothing is imputed.
- Idempotent: the cleaned and mapping tables are rebuilt (DELETE, then INSERT
  in key order) from the M2 tables and the CSVs in data/mappings/. Running it
  twice gives identical tables.
- Atomic: schema, mappings and cleaned tables are written in ONE transaction.
  If anything fails, the cleaned layer is left exactly as it was.
- Parameterised: every value reaches SQL through a ? or :name placeholder.
- Constants: all thresholds live in src/clean_config.py.
- Honest: prints flag counts, mapping matches and unmatched entries; exits 0
  with STATUS: COMPLETE, or 1 with STATUS: FAILED - rolled back.

Rule order: for every flag, the FIRST rule that applies wins. The order is the
order of the branches in the functions below and is documented, with counts, in
docs/cleaning_report.md section 5.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import math
import re
import sqlite3
import statistics
import sys
import traceback
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import clean_config as cfg
from db import (CLEAN_SCHEMA_PATH, CLEAN_TABLES, CLEANS, DB_PATH, M2_TABLES, MAPPING_TABLES,
                MAPPINGS_DIR, connect, row_counts, table_fingerprints)


class CleanError(Exception):
    """Something we refuse to build a cleaned layer from. The whole run is rolled back."""


# ============================================================ text helpers
TURKISH_FOLD = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
    "Ç": "c", "ç": "c", "Ö": "o", "ö": "o", "Ü": "u", "ü": "u",
})
WHITESPACE_RE = re.compile(r"\s+")


def normalise_text(value):
    """Cleaned display text: NFC, control characters -> space, whitespace runs -> one space, trimmed.

    Keeps everything else (case, punctuation, full-width and CJK characters): a
    title's spelling is the publisher's choice, so only invisible differences are removed.
    Returns None for NULL or text that is empty after this.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFC", value)
    text = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def name_key(value):
    """Matching key, used only to PROPOSE matches: never shown, never used as a name.

    NFKC (folds full-width letters and ligatures), then Turkish letters folded
    explicitly (str.lower() / casefold() turn 'İ' into 'i' + a combining dot),
    then casefold, then only letters and digits are kept. Returns None when no
    letter or digit is left (names such as '-' or '***'), so those never match.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value).translate(TURKISH_FOLD).casefold()
    text = "".join(ch for ch in text if ch.isalnum())
    return text or None


def text_flag(raw, cleaned) -> str:
    if raw is None:
        return "missing_not_provided"
    return "ok" if cleaned == raw else "text_normalised"


def upper_log_fence(values):
    """IQR upper fence on log10(value), over positive values. None if too few values."""
    logs = sorted(math.log10(v) for v in values if v is not None and v > 0)
    if len(logs) < cfg.MIN_VALUES_FOR_FENCE:
        return None, len(logs)
    q1, _, q3 = statistics.quantiles(logs, n=4, method="inclusive")
    return q3 + cfg.IQR_K * (q3 - q1), len(logs)


# ===================================================== static SQL (reads)
READ_APP = "SELECT app_key, steam_appid, name FROM app ORDER BY app_key"
READ_APP_DETAIL = """
SELECT d.app_key, a.steam_appid, d.store_name, d.app_type, d.is_free, d.release_date, d.release_date_text,
       d.coming_soon, d.price_currency, d.price_initial_cents, d.price_final_cents, d.discount_percent,
       d.platform_windows, d.platform_mac, d.platform_linux, d.metacritic_score,
       d.recommendations_total, d.retrieved_at
FROM app_detail AS d JOIN app AS a ON a.app_key = d.app_key
ORDER BY d.app_key
"""
READ_REVIEWS = """
SELECT app_key, snapshot_date, total_positive, total_negative, total_reviews,
       review_score, review_score_desc, retrieved_at
FROM review_snapshot ORDER BY app_key, snapshot_date
"""
READ_DEVELOPERS = "SELECT developer_key, name FROM developer ORDER BY developer_key"
READ_PUBLISHERS = "SELECT publisher_key, name FROM publisher ORDER BY publisher_key"
READ_GENRES = "SELECT genre_key, steam_genre_id, description FROM genre ORDER BY genre_key"
READ_CATEGORIES = "SELECT category_key, steam_category_id, description FROM category ORDER BY category_key"

# =================================================== static SQL (writes)
# Only cleaned and mapping tables are ever written. _check_write_targets()
# below refuses to import this module if a statement here names any other table.
DELETE_SQL = {
    "company_match": "DELETE FROM company_match",
    "genre_kind": "DELETE FROM genre_kind",
    "category_label": "DELETE FROM category_label",
    "app_identity": "DELETE FROM app_identity",
    "app_clean": "DELETE FROM app_clean",
    "app_detail_clean": "DELETE FROM app_detail_clean",
    "review_snapshot_clean": "DELETE FROM review_snapshot_clean",
    "developer_clean": "DELETE FROM developer_clean",
    "publisher_clean": "DELETE FROM publisher_clean",
    "genre_clean": "DELETE FROM genre_clean",
    "category_clean": "DELETE FROM category_clean",
}
# children first, so no foreign key ever points at a deleted row
DELETE_ORDER = ("review_snapshot_clean", "app_detail_clean", "app_clean", "developer_clean",
                "publisher_clean", "genre_clean", "category_clean",
                "app_identity", "company_match", "genre_kind", "category_label")

INSERT_SQL = {
    "company_match": """
        INSERT INTO company_match (role_a, name_a, role_b, name_b, method, decision, canonical,
                                   basis, decided_by, decided_at, note)
        VALUES (:role_a, :name_a, :role_b, :name_b, :method, :decision, :canonical,
                :basis, :decided_by, :decided_at, :note)""",
    "genre_kind": """
        INSERT INTO genre_kind (steam_genre_id, description, kind, decided_by, decided_at, note)
        VALUES (:steam_genre_id, :description, :kind, :decided_by, :decided_at, :note)""",
    "category_label": """
        INSERT INTO category_label (steam_category_id, description_raw, label, reason, basis,
                                    decided_by, decided_at, note)
        VALUES (:steam_category_id, :description_raw, :label, :reason, :basis,
                :decided_by, :decided_at, :note)""",
    "app_identity": """
        INSERT INTO app_identity (steam_appid, related_appid, method, decision, basis,
                                  decided_by, decided_at, note)
        VALUES (:steam_appid, :related_appid, :method, :decision, :basis,
                :decided_by, :decided_at, :note)""",
    "app_clean": """
        INSERT INTO app_clean (app_key, steam_appid, name_raw, name, name_flag, name_key)
        VALUES (:app_key, :steam_appid, :name_raw, :name, :name_flag, :name_key)""",
    "app_detail_clean": """
        INSERT INTO app_detail_clean (
            app_key, app_type, is_free, coming_soon, platform_windows, platform_mac, platform_linux,
            retrieved_at, store_name_raw, store_name, store_name_flag,
            release_date_raw, release_date, release_date_flag, release_year, release_precision,
            price_currency_raw, price_currency, price_initial_cents_raw, price_initial_cents,
            price_final_cents_raw, price_final_cents, discount_percent_raw, discount_percent, price_flag,
            metacritic_score_raw, metacritic_score, metacritic_score_flag,
            recommendations_total_raw, recommendations_total, recommendations_total_flag)
        VALUES (
            :app_key, :app_type, :is_free, :coming_soon, :platform_windows, :platform_mac, :platform_linux,
            :retrieved_at, :store_name_raw, :store_name, :store_name_flag,
            :release_date_raw, :release_date, :release_date_flag, :release_year, :release_precision,
            :price_currency_raw, :price_currency, :price_initial_cents_raw, :price_initial_cents,
            :price_final_cents_raw, :price_final_cents, :discount_percent_raw, :discount_percent, :price_flag,
            :metacritic_score_raw, :metacritic_score, :metacritic_score_flag,
            :recommendations_total_raw, :recommendations_total, :recommendations_total_flag)""",
    "review_snapshot_clean": """
        INSERT INTO review_snapshot_clean (
            app_key, snapshot_date, retrieved_at,
            total_positive_raw, total_positive, total_negative_raw, total_negative,
            total_reviews_raw, total_reviews, review_counts_flag,
            review_score_raw, review_score, review_score_desc_raw, review_score_desc, review_score_flag)
        VALUES (
            :app_key, :snapshot_date, :retrieved_at,
            :total_positive_raw, :total_positive, :total_negative_raw, :total_negative,
            :total_reviews_raw, :total_reviews, :review_counts_flag,
            :review_score_raw, :review_score, :review_score_desc_raw, :review_score_desc, :review_score_flag)""",
    "developer_clean": """
        INSERT INTO developer_clean (developer_key, name_raw, name, name_flag, name_key)
        VALUES (:key, :name_raw, :name, :name_flag, :name_key)""",
    "publisher_clean": """
        INSERT INTO publisher_clean (publisher_key, name_raw, name, name_flag, name_key)
        VALUES (:key, :name_raw, :name, :name_flag, :name_key)""",
    "genre_clean": """
        INSERT INTO genre_clean (genre_key, steam_genre_id, description_raw, description,
                                 description_flag, kind, kind_flag)
        VALUES (:genre_key, :steam_genre_id, :description_raw, :description,
                :description_flag, :kind, :kind_flag)""",
    "category_clean": """
        INSERT INTO category_clean (category_key, steam_category_id, description_raw, description,
                                    description_flag)
        VALUES (:category_key, :steam_category_id, :description_raw, :description, :description_flag)""",
}

# flag columns reported in the summary, per cleaned table
FLAG_COLUMNS = {
    "app_clean": ("name_flag",),
    "app_detail_clean": ("store_name_flag", "release_date_flag", "price_flag",
                         "metacritic_score_flag", "recommendations_total_flag"),
    "review_snapshot_clean": ("review_counts_flag", "review_score_flag"),
    "developer_clean": ("name_flag",),
    "publisher_clean": ("name_flag",),
    "genre_clean": ("description_flag", "kind_flag"),
    "category_clean": ("description_flag",),
}


FLAG_CHECK_RE = re.compile(r"(\w+_flag)\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*\1\s+IN\s*\(([^)]*)\)", re.IGNORECASE)


def allowed_flags(con, table: str) -> dict:
    """{flag column: allowed values in CHECK order}, read from the table's CREATE statement."""
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
    return {col: [v.strip().strip("'") for v in values.split(",")] for col, values in FLAG_CHECK_RE.findall(sql)}


def _check_write_targets() -> None:
    allowed = set(CLEAN_TABLES) | set(MAPPING_TABLES)
    target = re.compile(r"^\s*(?:INSERT\s+INTO|DELETE\s+FROM|UPDATE|DROP\s+\w+|REPLACE\s+INTO)\s+(\w+)",
                        re.IGNORECASE)
    for sql in list(DELETE_SQL.values()) + list(INSERT_SQL.values()):
        found = target.match(sql)
        if not found or found.group(1) not in allowed or found.group(1) in M2_TABLES:
            raise RuntimeError(f"clean.py may only write cleaned/mapping tables, not: {sql.strip()[:60]}")


_check_write_targets()


# ================================================================= mappings
MAPPING_FILES = {
    "company_match": ("company_match.csv",
                      ("role_a", "name_a", "role_b", "name_b", "method", "decision", "canonical",
                       "basis", "decided_by", "decided_at", "note")),
    "genre_kind": ("genre_kind.csv",
                   ("steam_genre_id", "description", "kind", "decided_by", "decided_at", "note")),
    "category_label": ("category_label.csv",
                       ("steam_category_id", "description_raw", "label", "reason", "basis",
                        "decided_by", "decided_at", "note")),
    "app_identity": ("app_identity.csv",
                     ("steam_appid", "related_appid", "method", "decision", "basis",
                      "decided_by", "decided_at", "note")),
}
INT_COLUMNS = {"steam_genre_id", "steam_category_id", "steam_appid", "related_appid"}


def read_mapping(mappings_dir: Path, table: str) -> list:
    """Read one decision CSV. Types and required fields are checked here; values
    allowed in each column are checked by the CHECK constraints on insert."""
    file_name, columns = MAPPING_FILES[table]
    path = mappings_dir / file_name
    if not path.is_file():
        raise CleanError(f"missing mapping file {path}")
    rows = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != columns:
            raise CleanError(f"{file_name}: columns must be {','.join(columns)}")
        for line, record in enumerate(reader, start=2):
            row = {}
            for column in columns:
                value = (record.get(column) or "").strip()
                if column in INT_COLUMNS:
                    if not value.isdigit():
                        raise CleanError(f"{file_name} line {line}: {column} must be a whole number")
                    row[column] = int(value)
                else:
                    row[column] = value or None
            for required in ("decided_by", "decided_at", "note"):
                if not row[required]:
                    raise CleanError(f"{file_name} line {line}: {required} is empty")
            try:
                date.fromisoformat(row["decided_at"])
            except ValueError:
                raise CleanError(f"{file_name} line {line}: decided_at must be YYYY-MM-DD") from None
            rows.append(row)
    return rows


def pair_key(role_a, name_a, role_b, name_b) -> tuple:
    return tuple(sorted(((role_a, name_a), (role_b, name_b))))


# ======================================================= run bookkeeping
@dataclass
class Report:
    rows: dict = field(default_factory=dict)            # cleaned table -> rows written
    stats: list = field(default_factory=list)           # lines about computed statistics
    mapping_lines: list = field(default_factory=list)   # match counts per step
    unmatched: list = field(default_factory=list)       # decisions / entries that matched nothing
    undecided: list = field(default_factory=list)       # proposals without a decision
    catalogue_key_groups: int = 0                       # catalogue name keys shared by > 1 app


def fetch_dicts(con, sql: str) -> list:
    cur = con.execute(sql)
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# ============================================================= clean: app
def clean_apps(con) -> list:
    rows = []
    for app_key, appid, raw in con.execute(READ_APP):
        cleaned = normalise_text(raw)
        rows.append({"app_key": app_key, "steam_appid": appid, "name_raw": raw, "name": cleaned,
                     "name_flag": text_flag(raw, cleaned), "name_key": name_key(cleaned)})
    return rows


# ====================================================== clean: app_detail
QUARTER_RE = re.compile(r"^Q([1-4])\s+(\d{4})$", re.IGNORECASE)
MONTH_YEAR_RE = re.compile(r"^([A-Za-z]{3,9})\.?,?\s+(\d{4})$")
YEAR_RE = re.compile(r"^(\d{4})$")
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def plausible_year(year: int, retrieved_year: int) -> bool:
    return cfg.MIN_PLAUSIBLE_RELEASE_YEAR <= year <= retrieved_year + cfg.MAX_ANNOUNCE_YEARS_AHEAD


def clean_release(text_raw, parsed, coming_soon, retrieved_at) -> dict:
    """Rule order (first match wins):
    1 missing_not_reported  no text and no date
    2 missing_not_announced text is 'Coming soon' / 'To be announced' / ...
    3 full date (parsed by M2): invalid_implausible | invalid_future (after retrieval, not coming soon)
                                | announced_future (after retrieval, coming soon) | ok
    4 partial_quarter 'Q4 2026'   5 partial_month 'October 2026'   6 partial_year '2026'
    7 invalid_unparseable       anything else
    The day is kept only when the source gave a full date; partial dates keep only their year.
    """
    out = {"release_date_raw": text_raw, "release_date": None, "release_year": None,
           "release_precision": "none"}
    day, year_now = retrieved_at[:10], int(retrieved_at[:4])
    text = normalise_text(text_raw)
    if text is None and parsed is None:
        return {**out, "release_date_flag": "missing_not_reported"}
    if text is not None and text.casefold() in cfg.NOT_ANNOUNCED_TEXTS:
        return {**out, "release_date_flag": "missing_not_announced"}
    if parsed is not None:
        year = int(parsed[:4])
        if not plausible_year(year, year_now):
            return {**out, "release_date_flag": "invalid_implausible"}
        if parsed > day and coming_soon != 1:
            return {**out, "release_date_flag": "invalid_future"}
        flag = "announced_future" if parsed > day else "ok"
        return {**out, "release_date": parsed, "release_year": year, "release_precision": "day",
                "release_date_flag": flag}
    for regex, precision, flag in ((QUARTER_RE, "quarter", "partial_quarter"),
                                   (MONTH_YEAR_RE, "month", "partial_month"),
                                   (YEAR_RE, "year", "partial_year")):
        found = regex.match(text)
        if not found:
            continue
        if precision == "month" and found.group(1)[:3].lower() not in MONTHS:
            continue
        year = int(found.group(found.lastindex))
        if not plausible_year(year, year_now):
            return {**out, "release_date_flag": "invalid_implausible"}
        return {**out, "release_year": year, "release_precision": precision, "release_date_flag": flag}
    return {**out, "release_date_flag": "invalid_unparseable"}


def price_problem(row) -> str | None:
    """Rules 1-5 of the price flag; None means the price object is usable."""
    if row["price_initial_cents"] is None and row["price_final_cents"] is None:
        if row["is_free"] == 1:
            return "missing_free"            # 1 a free game has no price_overview
        if row["coming_soon"] == 1:
            return "missing_unreleased"      # 2 an unreleased game has no price yet
        return "missing_not_reported"        # 3 released, paid, no price: not explained
    if row["price_currency"] != cfg.EXPECTED_CURRENCY:
        return "invalid_currency"            # 4 not the currency we asked for: unit unknown
    initial, final, discount = row["price_initial_cents"], row["price_final_cents"], row["discount_percent"]
    if initial is None or final is None or discount is None or final > initial or \
            abs(final - initial * (100 - discount) / 100) > cfg.PRICE_ROUNDING_TOLERANCE_CENTS:
        return "invalid_inconsistent"        # 5 parts do not add up
    return None


def clean_app_details(con, report: Report) -> list:
    source = fetch_dicts(con, READ_APP_DETAIL)
    problems = {r["app_key"]: price_problem(r) for r in source}
    price_fence, n_prices = upper_log_fence(
        [r["price_initial_cents"] for r in source if problems[r["app_key"]] is None])
    report.stats.append(
        f"price fence: log10(price_initial_cents) <= {price_fence:.3f} "
        f"(= {10 ** price_fence / 100:,.2f} USD) over {n_prices} usable prices" if price_fence is not None
        else f"price fence: not computed ({n_prices} usable prices < {cfg.MIN_VALUES_FOR_FENCE})")

    rows = []
    for r in source:
        store_name = normalise_text(r["store_name"])
        out = {
            "app_key": r["app_key"], "app_type": r["app_type"], "is_free": r["is_free"],
            "coming_soon": r["coming_soon"], "platform_windows": r["platform_windows"],
            "platform_mac": r["platform_mac"], "platform_linux": r["platform_linux"],
            "retrieved_at": r["retrieved_at"],
            "store_name_raw": r["store_name"], "store_name": store_name,
            "store_name_flag": text_flag(r["store_name"], store_name),
            **clean_release(r["release_date_text"], r["release_date"], r["coming_soon"], r["retrieved_at"]),
            "price_currency_raw": r["price_currency"], "price_initial_cents_raw": r["price_initial_cents"],
            "price_final_cents_raw": r["price_final_cents"], "discount_percent_raw": r["discount_percent"],
            "metacritic_score_raw": r["metacritic_score"], "metacritic_score": r["metacritic_score"],
            "metacritic_score_flag": "ok" if r["metacritic_score"] is not None else "missing_not_reported",
            "recommendations_total_raw": r["recommendations_total"],
            "recommendations_total": r["recommendations_total"],
            "recommendations_total_flag": ("ok" if r["recommendations_total"] is not None
                                           else "missing_not_reported"),
        }
        problem = problems[r["app_key"]]
        if problem is not None:
            out.update(price_currency=None, price_initial_cents=None, price_final_cents=None,
                       discount_percent=None, price_flag=problem)
        else:
            unusual = price_fence is not None and math.log10(r["price_initial_cents"]) > price_fence
            out.update(price_currency=r["price_currency"], price_initial_cents=r["price_initial_cents"],
                       price_final_cents=r["price_final_cents"], discount_percent=r["discount_percent"],
                       price_flag="unusual_high" if unusual else "ok")  # 6 unusual_high (kept) / 7 ok
        rows.append(out)
    return rows


# ========================================================= clean: reviews
def counts_problem(r) -> str | None:
    p, n, t = r["total_positive"], r["total_negative"], r["total_reviews"]
    if p is None or n is None or t is None:
        return "missing_not_reported"        # 1
    if p + n != t:
        return "invalid_sum_mismatch"        # 2 positive + negative must be the total
    return None


def score_flag(r, counts_ok: bool) -> str:
    """Rule order: 1 missing_not_reported, 2 missing_no_reviews, 3 missing_too_few_reviews,
    4 invalid_sentinel, 5 invalid_inconsistent, 6 ok."""
    t, s = r["total_reviews"], r["review_score"]
    if t is None or s is None:
        return "missing_not_reported"
    if t == 0:
        return "missing_no_reviews"          # Steam writes score 0, 'No user reviews'
    if t < cfg.MIN_REVIEWS_FOR_SCORE:
        return "missing_too_few_reviews"     # Steam writes score 0, 'N user reviews'
    if s == 0:
        return "invalid_sentinel"            # 0 with enough reviews: the 'no score' code where a score belongs
    band = cfg.SCORE_SHARE_BANDS.get(s)
    if counts_ok:
        share = 100.0 * r["total_positive"] / t
        if band is None or not (band[0] - cfg.SCORE_BAND_TOLERANCE_PCT <= share
                                < band[1] + cfg.SCORE_BAND_TOLERANCE_PCT):
            return "invalid_inconsistent"    # the label does not match the counts
    return "ok"


def clean_reviews(con, report: Report) -> list:
    source = fetch_dicts(con, READ_REVIEWS)
    problems = {(r["app_key"], r["snapshot_date"]): counts_problem(r) for r in source}
    fence, n_counts = upper_log_fence(
        [r["total_reviews"] for r in source if problems[(r["app_key"], r["snapshot_date"])] is None])
    report.stats.append(
        f"review fence: log10(total_reviews) <= {fence:.3f} (= {10 ** fence:,.0f} reviews) "
        f"over {n_counts} snapshots with reviews" if fence is not None
        else f"review fence: not computed ({n_counts} snapshots with reviews < {cfg.MIN_VALUES_FOR_FENCE})")

    rows = []
    for r in source:
        problem = problems[(r["app_key"], r["snapshot_date"])]
        out = {"app_key": r["app_key"], "snapshot_date": r["snapshot_date"], "retrieved_at": r["retrieved_at"],
               "total_positive_raw": r["total_positive"], "total_negative_raw": r["total_negative"],
               "total_reviews_raw": r["total_reviews"],
               "review_score_raw": r["review_score"], "review_score_desc_raw": r["review_score_desc"]}
        if problem is not None:
            out.update(total_positive=None, total_negative=None, total_reviews=None, review_counts_flag=problem)
        else:
            t = r["total_reviews"]
            if t == 0:
                flag = "zero_reviews"        # 3 a real zero: kept as 0
            elif fence is not None and math.log10(t) > fence:
                flag = "unusual_high"        # 4 kept
            else:
                flag = "ok"                  # 5
            out.update(total_positive=r["total_positive"], total_negative=r["total_negative"],
                       total_reviews=t, review_counts_flag=flag)
        sflag = score_flag(r, counts_ok=problem is None)
        keep = sflag == "ok"
        out.update(review_score=r["review_score"] if keep else None,
                   review_score_desc=normalise_text(r["review_score_desc"]) if keep else None,
                   review_score_flag=sflag)
        rows.append(out)
    return rows


# ======================================================= clean: companies
def company_proposals(names: list) -> dict:
    """names: [(role, name)]. Returns {pair_key: (method, ratio)} for different names
    that may be one company: equal name keys, or difflib ratio >= FUZZY_CUTOFF."""
    keyed = [(role, name, name_key(normalise_text(name))) for role, name in names]
    proposals = {}
    for i, (role_a, name_a, key_a) in enumerate(keyed):
        for role_b, name_b, key_b in keyed[i + 1:]:
            if name_a == name_b or key_a is None or key_b is None:
                continue
            if key_a == key_b:
                proposals[pair_key(role_a, name_a, role_b, name_b)] = ("normalised_key", 1.0)
            else:
                ratio = difflib.SequenceMatcher(None, key_a, key_b).ratio()
                if ratio >= cfg.FUZZY_CUTOFF:
                    proposals[pair_key(role_a, name_a, role_b, name_b)] = ("fuzzy", round(ratio, 3))
    return proposals


def clean_companies(con, decisions: list, report: Report):
    developers = con.execute(READ_DEVELOPERS).fetchall()
    publishers = con.execute(READ_PUBLISHERS).fetchall()
    present = {("developer", n) for _, n in developers} | {("publisher", n) for _, n in publishers}

    exact = len({n for _, n in developers} & {n for _, n in publishers})
    proposals = company_proposals(sorted(present))
    decided = {pair_key(d["role_a"], d["name_a"], d["role_b"], d["name_b"]): d for d in decisions}

    alias = {}  # (role, name) -> canonical name
    for key, d in sorted(decided.items()):
        if not (key[0] in present and key[1] in present):
            report.unmatched.append(f"company_match: {key[0][0]} {key[0][1]!r} / {key[1][0]} {key[1][1]!r} "
                                    "(a name is not in the database)")
            continue
        if d["decision"] == "same":
            for role, name in key:
                if name != d["canonical"]:
                    alias[(role, name)] = d["canonical"]
    for key, (method, ratio) in sorted(proposals.items()):
        if key not in decided:
            report.undecided.append(f"company names {key[0][0]} {key[0][1]!r} / {key[1][0]} {key[1][1]!r} "
                                    f"({method}, ratio {ratio})")
    by_method = Counter(method for method, _ in proposals.values())
    report.mapping_lines.append(
        f"company names: exact {exact} publisher names = developer names · normalised key "
        f"{by_method['normalised_key']} proposal(s) · fuzzy (>= {cfg.FUZZY_CUTOFF}) {by_method['fuzzy']} "
        f"proposal(s) · decided {sum(k in decided for k in proposals)} of {len(proposals)} "
        f"· aliases applied {len(alias)}")

    def rows_for(role, source):
        rows = []
        for key, raw in source:
            if (role, raw) in alias:
                cleaned, flag = alias[(role, raw)], "alias_mapped"
            else:
                cleaned = normalise_text(raw)
                flag = "ok" if cleaned == raw else "text_normalised"
            rows.append({"key": key, "name_raw": raw, "name": cleaned, "name_flag": flag,
                         "name_key": name_key(cleaned)})
        return rows

    return rows_for("developer", developers), rows_for("publisher", publishers)


# ================================================ clean: genres/categories
def clean_genres(con, kinds: list, report: Report) -> list:
    by_id = {k["steam_genre_id"]: k for k in kinds}
    rows, mapped = [], 0
    for genre_key, genre_id, raw in con.execute(READ_GENRES).fetchall():
        cleaned = normalise_text(raw)
        decision = by_id.get(genre_id)
        if decision is not None:
            mapped += 1
            if decision["description"] != cleaned:
                report.unmatched.append(f"genre_kind: id {genre_id} decided as {decision['description']!r}, "
                                        f"database now says {cleaned!r} (decision still applied - review it)")
        rows.append({"genre_key": genre_key, "steam_genre_id": genre_id, "description_raw": raw,
                     "description": cleaned,
                     "description_flag": "missing_not_reported" if raw is None else
                     ("ok" if cleaned == raw else "text_normalised"),
                     "kind": decision["kind"] if decision else None,
                     "kind_flag": "mapped" if decision else "unmapped"})
    unmapped = [r["steam_genre_id"] for r in rows if r["kind_flag"] == "unmapped"]
    report.mapping_lines.append(f"genres: kind mapped {mapped} of {len(rows)} "
                                f"(unmapped: {', '.join(map(str, unmapped)) or 'none'})")
    if unmapped:
        report.undecided.append(f"genre ids without a genre_kind row: {unmapped}")
    return rows


def clean_categories(con, labels: list, report: Report) -> list:
    by_id = {lab["steam_category_id"]: lab for lab in labels}
    rows, applied = [], 0
    for category_key, category_id, raw in con.execute(READ_CATEGORIES).fetchall():
        cleaned = normalise_text(raw)
        flag = "missing_not_reported" if raw is None else ("ok" if cleaned == raw else "text_normalised")
        decision = by_id.get(category_id)
        if decision is not None:
            if decision["description_raw"] == raw:
                cleaned, flag = decision["label"], decision["reason"]
                applied += 1
            else:
                report.unmatched.append(f"category_label: id {category_id} expects {decision['description_raw']!r}, "
                                        f"database says {raw!r} (not applied)")
        rows.append({"category_key": category_key, "steam_category_id": category_id,
                     "description_raw": raw, "description": cleaned, "description_flag": flag})
    # proposals: labels that still look like placeholders, or that two ids share
    for r in rows:
        if r["description"] and r["description"].startswith("#"):
            report.undecided.append(f"category {r['steam_category_id']}: placeholder label {r['description']!r}")
    shared = defaultdict(list)
    for r in rows:
        shared[r["description"]].append(r["steam_category_id"])
    for label, ids in sorted(shared.items(), key=lambda x: str(x[0])):
        if label is not None and len(ids) > 1:
            report.undecided.append(f"categories {ids} share the label {label!r}")
    report.mapping_lines.append(f"category labels: applied {applied} of {len(labels)} decision(s)")
    return rows


# ==================================================== identity proposals
def identity_check(app_rows: list, detail_rows: list, decisions: list, report: Report) -> None:
    """Propose catalogue apps whose name key equals a sampled app's name key."""
    by_key = defaultdict(list)
    for r in app_rows:
        if r["name_key"] is not None:
            by_key[r["name_key"]].append(r["steam_appid"])
    appid_of = {r["app_key"]: r["steam_appid"] for r in app_rows}
    app_name_key = {r["app_key"]: r["name_key"] for r in app_rows}
    proposals = set()
    for d in detail_rows:
        appid = appid_of[d["app_key"]]
        for key in {app_name_key[d["app_key"]], name_key(d["store_name"])} - {None}:
            proposals |= {(appid, other) for other in by_key.get(key, []) if other != appid}
    decided = {(d["steam_appid"], d["related_appid"]): d for d in decisions}
    for pair in sorted(set(decided) - proposals):
        report.unmatched.append(f"app_identity: {pair[0]} / {pair[1]} is no longer proposed by the name key")
    for pair in sorted(proposals - set(decided)):
        report.undecided.append(f"apps {pair[0]} / {pair[1]} share a name key")
    same = sum(1 for d in decisions if d["decision"] == "same")
    report.mapping_lines.append(
        f"app identity: {len(proposals)} name-key proposal(s) for sampled apps · decided "
        f"{len(proposals & set(decided))} · same {same} · separate {len(decisions) - same}")
    report.catalogue_key_groups = sum(1 for v in by_key.values() if len(v) > 1)


# ===================================================================== run
def schema_statements(text: str):
    """Split sql/clean_schema.sql into complete statements, so they run inside our
    transaction (executescript() would commit first). PRAGMAs are skipped: connect()
    already sets foreign_keys, and a PRAGMA cannot change it inside a transaction."""
    buffer = ""
    for line in text.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if not statement.upper().startswith("PRAGMA"):
                yield statement
    leftover = [l for l in buffer.splitlines() if l.strip() and not l.strip().startswith("--")]
    if leftover:
        raise CleanError("sql/clean_schema.sql ends with an incomplete statement")


@dataclass
class RunResult:
    ok: bool
    report: str
    error: str = ""


def build(con, mappings_dir: Path, report: Report, flag_counts: dict, year_matrix: Counter) -> None:
    """Everything that writes. Called inside one transaction by run()."""
    for statement in schema_statements(CLEAN_SCHEMA_PATH.read_text(encoding="utf-8")):
        con.execute(statement)
    for table in DELETE_ORDER:
        con.execute(DELETE_SQL[table])

    mappings = {t: read_mapping(mappings_dir, t) for t in MAPPING_TABLES}
    for table in ("genre_kind", "category_label", "app_identity", "company_match"):
        con.executemany(INSERT_SQL[table], mappings[table])

    built = {"app_clean": clean_apps(con),
             "app_detail_clean": clean_app_details(con, report),
             "review_snapshot_clean": clean_reviews(con, report)}
    built["developer_clean"], built["publisher_clean"] = clean_companies(con, mappings["company_match"], report)
    built["genre_clean"] = clean_genres(con, mappings["genre_kind"], report)
    built["category_clean"] = clean_categories(con, mappings["category_label"], report)
    identity_check(built["app_clean"], built["app_detail_clean"], mappings["app_identity"], report)

    for table in CLEAN_TABLES:
        con.executemany(INSERT_SQL[table], built[table])
        report.rows[table] = len(built[table])
        allowed = allowed_flags(con, table)
        flag_counts[table] = {}
        for col in FLAG_COLUMNS[table]:
            counter = Counter(r[col] for r in built[table])
            flag_counts[table][col] = [(flag, counter.get(flag, 0)) for flag in allowed.get(col, sorted(counter))]
    years = {r["app_key"]: r["release_year"] for r in built["app_detail_clean"]}
    for r in built["review_snapshot_clean"]:
        year_matrix[(years.get(r["app_key"]), r["review_score_flag"])] += 1

    counts = row_counts(con)
    for clean_table, raw_table in CLEANS.items():
        if counts[clean_table] != counts[raw_table]:
            raise CleanError(f"{clean_table} has {counts[clean_table]:,} rows but {raw_table} has "
                             f"{counts[raw_table]:,}")


def run(db_path: Path = DB_PATH, mappings_dir: Path = MAPPINGS_DIR, echo: bool = True) -> RunResult:
    db_path, mappings_dir = Path(db_path), Path(mappings_dir)
    report, flag_counts, year_matrix = Report(), {}, Counter()
    counts, m2_same, fk_problems, error = {}, None, None, ""
    if not db_path.is_file():
        error = f"CleanError: {db_path} does not exist - build it first with: python src/load.py"
    else:
        con = connect(db_path)
        try:
            m2_before = table_fingerprints(con, M2_TABLES)
            if len(m2_before) != len(M2_TABLES):
                error = "CleanError: the M2 tables are missing - run python src/load.py first"
            else:
                try:
                    with con:                            # ONE transaction: commit on success, roll back on error
                        con.execute("BEGIN")
                        build(con, mappings_dir, report, flag_counts, year_matrix)
                        fk_problems = con.execute("PRAGMA foreign_key_check").fetchall()
                        if fk_problems:
                            raise CleanError(f"foreign key check reported {len(fk_problems)} violation(s)")
                        if table_fingerprints(con, M2_TABLES) != m2_before:
                            raise CleanError("an M2 table changed during cleaning")
                except Exception as exc:                 # noqa: BLE001 - reported, and rolled back
                    error = f"{type(exc).__name__}: {exc}"
                    if not isinstance(exc, (CleanError, sqlite3.Error)):
                        traceback.print_exc()
                m2_same = table_fingerprints(con, M2_TABLES) == m2_before
                if not m2_same and not error:
                    error = "CleanError: an M2 table changed"
            counts = row_counts(con)
        finally:
            con.close()

    text = summary(db_path, counts, report, flag_counts, year_matrix, m2_same, fk_problems, error)
    if echo:
        print(text)
    return RunResult(ok=not error, report=text, error=error)


def display_path(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def summary(db_path, counts, report, flag_counts, year_matrix, m2_same, fk_problems, error) -> str:
    lines = [f"clean.py - cleaned layer in {display_path(db_path)}"]
    if not error:
        for table in CLEAN_TABLES:
            raw = CLEANS[table]
            lines.append(f"{table + ':':<23}{counts.get(table, 0):,} rows (raw {raw}: {counts.get(raw, 0):,})")
            for col, pairs in flag_counts.get(table, {}).items():
                lines.append(f"  {col:<29}" + " · ".join(f"{flag} {n:,}" for flag, n in pairs))
        lines.append("")
        lines.append("review_score_flag by release year (app_detail_clean.release_year):")
        flags = sorted({f for _, f in year_matrix})
        lines.append(f"  {'year':<8}" + "".join(f"{f:>25}" for f in flags))
        for year in sorted({y for y, _ in year_matrix}, key=lambda y: (y is None, y or 0)):
            label = "none" if year is None else str(year)
            lines.append(f"  {label:<8}" + "".join(f"{year_matrix[(year, f)]:>25}" for f in flags))
        lines.append("")
        lines.append("statistics:")
        lines += [f"  {s}" for s in report.stats]
        lines.append("mappings:")
        lines.append("  rows loaded: " + " · ".join(f"{t} {counts.get(t, 0)}" for t in MAPPING_TABLES))
        lines += [f"  {m}" for m in report.mapping_lines]
        lines.append(f"  catalogue name-key groups with more than one app: {report.catalogue_key_groups:,} "
                     "(reported, not merged: only sampled apps are checked)")
        lines.append(f"  decisions that matched nothing: {len(report.unmatched)}")
        lines += [f"    - {u}" for u in report.unmatched]
        lines.append(f"  proposals without a decision: {len(report.undecided)}")
        lines += [f"    - {u}" for u in report.undecided]
    lines.append(f"M2 tables unchanged:   {'yes (sha256 of all 12 M2 tables before = after)' if m2_same else 'NO' if m2_same is False else 'not checked'}")
    if error:
        lines.append(f"foreign_key_check:     {'not reached' if fk_problems is None else f'{len(fk_problems)} violation(s)'}")
        lines.append(f"error:                 {error}")
        lines.append("STATUS: FAILED - rolled back, the cleaned layer is as it was before this run")
    else:
        lines.append("foreign_key_check:     OK")
        lines.append("STATUS: COMPLETE")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Build the cleaned layer from the M2 tables.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"database file (default: {DB_PATH})")
    parser.add_argument("--mappings", type=Path, default=MAPPINGS_DIR,
                        help=f"folder with the decision CSVs (default: {MAPPINGS_DIR})")
    args = parser.parse_args()
    return 0 if run(args.db, args.mappings).ok else 1


if __name__ == "__main__":
    sys.exit(main())
