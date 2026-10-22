"""Load the landed raw Steam files (data/raw/) into the SQLite database.

Run from the repository root:

    python src/load.py                       # builds / updates data/steam_catalog.db
    python src/load.py --db x.db --raw dir   # other database / raw folder

Never calls the API: M1 (src/ingest.py) lands the data, this script only
turns the landed files into a database. Running it on a fresh clone creates
the database from scratch; running it again leaves the database unchanged.

How it behaves
- Idempotent:  every write is INSERT ... ON CONFLICT ... DO UPDATE on the
               natural key, so surrogate keys never change.
- Atomic:      the whole load is one transaction. If anything fails, nothing
               from this run is written.
- Deduplicates by key, not by row: when the same key was landed more than
               once, the most recently retrieved version wins (retrieved_at,
               taken from the file name), never the order files are read in.
- Validates before inserting: values that do not fit their column become
               NULL and are counted in the run summary.
- Unknown references (an app that is in no landed app list) and responses
               with no usable data go to the quarantine table: never dropped,
               never a crash.
- A raw file that is not valid JSON, or whose name has no UTC timestamp,
               stops the run (rolled back): the landed data is damaged and we
               refuse to build a database from part of it.
- Every value reaches SQL through a ? or :name placeholder.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from db import DB_PATH, RAW_DIR, apply_schema, connect, row_counts

SOURCES = ("applist", "appdetails", "appreviews")
FILE_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)_(.+)\.json$")  # <UTC stamp>_<ref>.json (src/ingest.py)
APPID_REF_RE = re.compile(r"^(\d+)(?:_\d+)?$")             # "<appid>" or "<appid>_<n>" on a name clash
INT_TEXT_RE = re.compile(r"^[+-]?\d+$")
ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
DATE_MDY_RE = re.compile(r"^([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})$")   # Oct 17, 2017
DATE_DMY_RE = re.compile(r"^(\d{1,2}) ([A-Za-z]{3,9})\.?,? (\d{4})$")   # 17 Oct, 2017


class LoadError(Exception):
    """Landed data we refuse to load. The whole run is rolled back."""


# =============================================================== SQL (static)
UPSERT_APP = """
INSERT INTO app (steam_appid, name, last_modified_at, price_change_number, retrieved_at)
VALUES (:steam_appid, :name, :last_modified_at, :price_change_number, :retrieved_at)
ON CONFLICT (steam_appid) DO UPDATE SET
    name                = excluded.name,
    last_modified_at    = excluded.last_modified_at,
    price_change_number = excluded.price_change_number,
    retrieved_at        = excluded.retrieved_at
WHERE excluded.retrieved_at >= app.retrieved_at
"""

UPSERT_APP_DETAIL = """
INSERT INTO app_detail (
    app_key, store_name, app_type, is_free, required_age, release_date, release_date_text,
    coming_soon, price_currency, price_initial_cents, price_final_cents, discount_percent,
    platform_windows, platform_mac, platform_linux, metacritic_score, recommendations_total,
    retrieved_at)
VALUES (
    :app_key, :store_name, :app_type, :is_free, :required_age, :release_date, :release_date_text,
    :coming_soon, :price_currency, :price_initial_cents, :price_final_cents, :discount_percent,
    :platform_windows, :platform_mac, :platform_linux, :metacritic_score, :recommendations_total,
    :retrieved_at)
ON CONFLICT (app_key) DO UPDATE SET
    store_name            = excluded.store_name,
    app_type              = excluded.app_type,
    is_free               = excluded.is_free,
    required_age          = excluded.required_age,
    release_date          = excluded.release_date,
    release_date_text     = excluded.release_date_text,
    coming_soon           = excluded.coming_soon,
    price_currency        = excluded.price_currency,
    price_initial_cents   = excluded.price_initial_cents,
    price_final_cents     = excluded.price_final_cents,
    discount_percent      = excluded.discount_percent,
    platform_windows      = excluded.platform_windows,
    platform_mac          = excluded.platform_mac,
    platform_linux        = excluded.platform_linux,
    metacritic_score      = excluded.metacritic_score,
    recommendations_total = excluded.recommendations_total,
    retrieved_at          = excluded.retrieved_at
WHERE excluded.retrieved_at >= app_detail.retrieved_at
"""

UPSERT_GENRE = """
INSERT INTO genre (steam_genre_id, description, retrieved_at) VALUES (?, ?, ?)
ON CONFLICT (steam_genre_id) DO UPDATE SET
    description = excluded.description, retrieved_at = excluded.retrieved_at
WHERE excluded.retrieved_at >= genre.retrieved_at
"""

UPSERT_CATEGORY = """
INSERT INTO category (steam_category_id, description, retrieved_at) VALUES (?, ?, ?)
ON CONFLICT (steam_category_id) DO UPDATE SET
    description = excluded.description, retrieved_at = excluded.retrieved_at
WHERE excluded.retrieved_at >= category.retrieved_at
"""

UPSERT_DEVELOPER = """
INSERT INTO developer (name, retrieved_at) VALUES (?, ?)
ON CONFLICT (name) DO UPDATE SET retrieved_at = excluded.retrieved_at
WHERE excluded.retrieved_at >= developer.retrieved_at
"""

UPSERT_PUBLISHER = """
INSERT INTO publisher (name, retrieved_at) VALUES (?, ?)
ON CONFLICT (name) DO UPDATE SET retrieved_at = excluded.retrieved_at
WHERE excluded.retrieved_at >= publisher.retrieved_at
"""

# Bridge rows of one app are re-synced from its winning store page: delete the
# app's links, then insert the current ones. Bridge tables have no surrogate
# key (composite primary key, WITHOUT ROWID), so no key ever changes.
BRIDGE_SQL = {
    "genre": (
        "DELETE FROM app_genre WHERE app_key = ?",
        "INSERT INTO app_genre (app_key, genre_key, retrieved_at) VALUES (?, ?, ?) "
        "ON CONFLICT (app_key, genre_key) DO UPDATE SET retrieved_at = excluded.retrieved_at",
    ),
    "category": (
        "DELETE FROM app_category WHERE app_key = ?",
        "INSERT INTO app_category (app_key, category_key, retrieved_at) VALUES (?, ?, ?) "
        "ON CONFLICT (app_key, category_key) DO UPDATE SET retrieved_at = excluded.retrieved_at",
    ),
    "developer": (
        "DELETE FROM app_developer WHERE app_key = ?",
        "INSERT INTO app_developer (app_key, developer_key, retrieved_at) VALUES (?, ?, ?) "
        "ON CONFLICT (app_key, developer_key) DO UPDATE SET retrieved_at = excluded.retrieved_at",
    ),
    "publisher": (
        "DELETE FROM app_publisher WHERE app_key = ?",
        "INSERT INTO app_publisher (app_key, publisher_key, retrieved_at) VALUES (?, ?, ?) "
        "ON CONFLICT (app_key, publisher_key) DO UPDATE SET retrieved_at = excluded.retrieved_at",
    ),
}

UPSERT_REVIEW = """
INSERT INTO review_snapshot (
    app_key, snapshot_date, total_positive, total_negative, total_reviews,
    review_score, review_score_desc, retrieved_at)
VALUES (
    :app_key, :snapshot_date, :total_positive, :total_negative, :total_reviews,
    :review_score, :review_score_desc, :retrieved_at)
ON CONFLICT (app_key, snapshot_date) DO UPDATE SET
    total_positive    = excluded.total_positive,
    total_negative    = excluded.total_negative,
    total_reviews     = excluded.total_reviews,
    review_score      = excluded.review_score,
    review_score_desc = excluded.review_score_desc,
    retrieved_at      = excluded.retrieved_at
WHERE excluded.retrieved_at >= review_snapshot.retrieved_at
"""

UPSERT_QUARANTINE = """
INSERT INTO quarantine (source, source_file, record_ref, steam_appid, reason, detail, retrieved_at)
VALUES (:source, :source_file, :record_ref, :steam_appid, :reason, :detail, :retrieved_at)
ON CONFLICT (source_file, record_ref) DO UPDATE SET
    source       = excluded.source,
    steam_appid  = excluded.steam_appid,
    reason       = excluded.reason,
    detail       = excluded.detail,
    retrieved_at = excluded.retrieved_at
"""

# A record quarantined because its app was unknown is resolved once the app exists.
RESOLVE_QUARANTINE = """
DELETE FROM quarantine
WHERE reason = ? AND steam_appid IN (SELECT steam_appid FROM app)
"""


# ================================================================ raw files
@dataclass
class RawFile:
    source: str
    path: Path
    rel: str            # path under the raw folder, forward slashes (stored in quarantine)
    retrieved_at: str   # ISO 8601 UTC, from the file name
    ref: str            # the part of the file name after the timestamp


def stamp_to_iso(stamp: str) -> str:
    """'20261003T113818Z' -> '2026-10-03T11:38:18Z' (raises ValueError if not a real time)."""
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(ISO_UTC)


def list_raw_files(raw_dir: Path) -> list:
    files = []
    for source in SOURCES:
        folder = raw_dir / source
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            rel = path.relative_to(raw_dir).as_posix()
            match = FILE_NAME_RE.match(path.name)
            try:
                retrieved_at = stamp_to_iso(match.group(1)) if match else None
            except ValueError:
                retrieved_at = None
            if retrieved_at is None:
                raise LoadError(
                    f"{rel}: file name does not start with a UTC timestamp "
                    "(YYYYMMDDTHHMMSSZ_...), so we cannot tell when it was retrieved"
                )
            files.append(RawFile(source, path, rel, retrieved_at, match.group(2)))
    if not files:
        raise LoadError(f"no raw files under {raw_dir} - run python src/ingest.py first")
    return files


def read_json(raw: RawFile):
    try:
        return json.loads(raw.path.read_bytes())
    except (ValueError, UnicodeDecodeError) as exc:
        raise LoadError(f"{raw.rel}: not valid JSON ({exc}) - the landed file is damaged") from None


def appid_from_ref(raw: RawFile):
    match = APPID_REF_RE.match(raw.ref)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def newer(candidate: tuple, current) -> bool:
    """Version order: retrieved_at first, then file path (deterministic tie-break)."""
    return current is None or candidate > current


# ============================================================== validation
class Cleaner:
    """Converts raw values to column types; counts every value it had to change."""

    def __init__(self):
        self.counts = Counter()

    def note(self, what: str) -> None:
        self.counts[what] += 1

    def integer(self, value, name: str, low=None, high=None):
        if value is None:
            return None
        if isinstance(value, bool):
            self.note(f"{name}: true/false where a number belongs -> NULL")
            return None
        if isinstance(value, int):
            result = value
        elif isinstance(value, float) and value.is_integer():
            result = int(value)
            self.note(f"{name}: decimal -> integer")
        elif isinstance(value, str) and INT_TEXT_RE.match(value.strip()):
            result = int(value.strip())
            self.note(f"{name}: text -> integer")
        else:
            self.note(f"{name}: non-numeric -> NULL")
            return None
        if (low is not None and result < low) or (high is not None and result > high):
            self.note(f"{name}: out of range -> NULL")
            return None
        return result

    def flag(self, value, name: str):
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int) and value in (0, 1):
            self.note(f"{name}: number -> true/false")
            return value
        self.note(f"{name}: not true/false -> NULL")
        return None

    def text(self, value, name: str):
        if value is None:
            return None
        if not isinstance(value, str):
            self.note(f"{name}: not text -> NULL")
            return None
        if not value.strip():
            self.note(f"{name}: empty text -> NULL")
            return None
        return value

    def unix_to_iso(self, value, name: str):
        seconds = self.integer(value, name, low=1)
        if seconds is None:
            return None
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(ISO_UTC)
        except (OverflowError, OSError, ValueError):
            self.note(f"{name}: not a valid unix time -> NULL")
            return None

    def release_date(self, value, name: str):
        """'Oct 17, 2017' -> '2017-10-17'. Partial dates ('Q4 2026', 'Coming soon') -> NULL."""
        text = value.strip() if isinstance(value, str) else None
        if not text:
            return None
        found = DATE_MDY_RE.match(text)
        parts = (found.group(1), found.group(2), found.group(3)) if found else None
        if parts is None:
            found = DATE_DMY_RE.match(text)
            parts = (found.group(2), found.group(1), found.group(3)) if found else None
        if parts is not None:
            month = MONTHS.get(parts[0][:3].lower())
            try:
                if month is not None:
                    return date(int(parts[2]), month, int(parts[1])).isoformat()
            except ValueError:
                pass
        self.note(f"{name}: not a full date (e.g. 'Coming soon', 'Q4 2026') -> NULL")
        return None


@dataclass
class Staging:
    cleaner: Cleaner = field(default_factory=Cleaner)
    superseded: Counter = field(default_factory=Counter)
    quarantine: list = field(default_factory=list)

    def reject(self, raw: RawFile, record_ref: str, appid, reason: str, detail: str) -> None:
        self.quarantine.append({
            "source": raw.source, "source_file": raw.rel, "record_ref": record_ref,
            "steam_appid": appid, "reason": reason, "detail": detail,
            "retrieved_at": raw.retrieved_at,
        })


# ================================================================== app list
def load_applist(con, files: list, st: Staging) -> None:
    winners = {}  # steam_appid -> ((retrieved_at, rel), raw file, entry)
    for raw in files:
        payload = read_json(raw)
        body = payload.get("response") if isinstance(payload, dict) else None
        apps = body.get("apps") if isinstance(body, dict) else None
        if not isinstance(apps, list):
            st.reject(raw, "page", None, "unusable_structure", "expected {'response': {'apps': [...]}}")
            continue
        for index, entry in enumerate(apps):
            appid = entry.get("appid") if isinstance(entry, dict) else None
            if isinstance(appid, bool) or not isinstance(appid, int) or appid <= 0:
                st.reject(raw, f"apps[{index}]", None, "no_valid_appid", repr(entry)[:200])
                continue
            version = (raw.retrieved_at, raw.rel)
            current = winners.get(appid)
            if current is not None:
                st.superseded["app"] += 1
            if newer(version, current[0] if current else None):
                winners[appid] = (version, raw, entry)

    c = st.cleaner
    rows = [
        {
            "steam_appid": appid,
            "name": c.text(entry.get("name"), "app.name"),
            "last_modified_at": c.unix_to_iso(entry.get("last_modified"), "app.last_modified"),
            "price_change_number": c.integer(entry.get("price_change_number"),
                                             "app.price_change_number", low=0),
            "retrieved_at": raw.retrieved_at,
        }
        for appid, (_, raw, entry) in sorted(winners.items())
    ]
    con.executemany(UPSERT_APP, rows)


def app_keys(con) -> dict:
    return dict(con.execute("SELECT steam_appid, app_key FROM app"))


# =============================================================== app details
def load_appdetails(con, files: list, st: Staging) -> None:
    keys = app_keys(con)
    winners = {}  # steam_appid -> ((retrieved_at, rel), raw file, data)
    for raw in files:
        payload = read_json(raw)
        appid = appid_from_ref(raw)
        if appid is None:
            st.reject(raw, raw.ref, None, "no_valid_appid", "file name has no appid")
            continue
        entry = payload.get(str(appid)) if isinstance(payload, dict) else None
        data = entry.get("data") if isinstance(entry, dict) else None
        if not isinstance(entry, dict):
            st.reject(raw, raw.ref, appid, "unusable_structure", "no entry for this appid in the response")
        elif entry.get("success") is not True:
            st.reject(raw, raw.ref, appid, "no_store_data", "success=false: Steam has no store page for it")
        elif not isinstance(data, dict) or not data:
            st.reject(raw, raw.ref, appid, "no_store_data", "success=true but data is empty")
        elif isinstance(data.get("steam_appid"), int) and data["steam_appid"] != appid:
            st.reject(raw, raw.ref, appid, "appid_mismatch",
                      f"store returned the page of appid {data['steam_appid']}")
        elif appid not in keys:
            st.reject(raw, raw.ref, appid, "unknown_app", "appid is not in any landed app list")
        else:
            version = (raw.retrieved_at, raw.rel)
            current = winners.get(appid)
            if current is not None:
                st.superseded["app_detail"] += 1
            if newer(version, current[0] if current else None):
                winners[appid] = (version, raw, data)

    c = st.cleaner
    details, links = [], []
    genres, categories, developers, publishers = {}, {}, {}, {}  # natural key -> (retrieved_at, description)
    for appid, (_, raw, data) in sorted(winners.items()):
        when = raw.retrieved_at
        release = data.get("release_date") if isinstance(data.get("release_date"), dict) else {}
        price = data.get("price_overview") if isinstance(data.get("price_overview"), dict) else {}
        platforms = data.get("platforms") if isinstance(data.get("platforms"), dict) else {}
        metacritic = data.get("metacritic") if isinstance(data.get("metacritic"), dict) else {}
        recommendations = data.get("recommendations") if isinstance(data.get("recommendations"), dict) else {}
        details.append({
            "app_key": keys[appid],
            "store_name": c.text(data.get("name"), "app_detail.store_name"),
            "app_type": c.text(data.get("type"), "app_detail.app_type"),
            "is_free": c.flag(data.get("is_free"), "app_detail.is_free"),
            "required_age": c.integer(data.get("required_age"), "app_detail.required_age", low=0),
            "release_date": c.release_date(release.get("date"), "app_detail.release_date"),
            "release_date_text": c.text(release.get("date"), "app_detail.release_date_text"),
            "coming_soon": c.flag(release.get("coming_soon"), "app_detail.coming_soon"),
            "price_currency": c.text(price.get("currency"), "app_detail.price_currency"),
            "price_initial_cents": c.integer(price.get("initial"), "app_detail.price_initial_cents", low=0),
            "price_final_cents": c.integer(price.get("final"), "app_detail.price_final_cents", low=0),
            "discount_percent": c.integer(price.get("discount_percent"), "app_detail.discount_percent",
                                          low=0, high=100),
            "platform_windows": c.flag(platforms.get("windows"), "app_detail.platform_windows"),
            "platform_mac": c.flag(platforms.get("mac"), "app_detail.platform_mac"),
            "platform_linux": c.flag(platforms.get("linux"), "app_detail.platform_linux"),
            "metacritic_score": c.integer(metacritic.get("score"), "app_detail.metacritic_score",
                                          low=0, high=100),
            "recommendations_total": c.integer(recommendations.get("total"),
                                               "app_detail.recommendations_total", low=0),
            "retrieved_at": when,
        })
        app_links = {"genre": set(), "category": set(), "developer": set(), "publisher": set()}
        for kind, items, target in (("genre", data.get("genres"), genres),
                                    ("category", data.get("categories"), categories)):
            for item in items if isinstance(items, list) else []:
                raw_id = item.get("id") if isinstance(item, dict) else None
                # Steam sends genre ids as text ("23") and category ids as numbers.
                if isinstance(raw_id, str) and raw_id.strip().isdigit():
                    raw_id = int(raw_id.strip())
                if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                    c.note(f"{kind}: id is not a number -> link skipped")
                    continue
                description = item.get("description") if isinstance(item.get("description"), str) else None
                if newer((when, description or ""), target.get(raw_id)):
                    target[raw_id] = (when, description or "")
                app_links[kind].add(raw_id)
        for kind, names, target in (("developer", data.get("developers"), developers),
                                    ("publisher", data.get("publishers"), publishers)):
            for name in names if isinstance(names, list) else []:
                name = name.strip() if isinstance(name, str) else ""
                if not name:
                    c.note(f"{kind}: empty name -> link skipped")
                    continue
                if newer((when, ""), target.get(name)):
                    target[name] = (when, "")
                app_links[kind].add(name)
        links.append((keys[appid], when, app_links))

    # entities first, then the details, then the links (foreign keys hold at every step)
    con.executemany(UPSERT_GENRE, [(k, d or None, w) for k, (w, d) in sorted(genres.items())])
    con.executemany(UPSERT_CATEGORY, [(k, d or None, w) for k, (w, d) in sorted(categories.items())])
    con.executemany(UPSERT_DEVELOPER, [(k, w) for k, (w, _) in sorted(developers.items())])
    con.executemany(UPSERT_PUBLISHER, [(k, w) for k, (w, _) in sorted(publishers.items())])
    con.executemany(UPSERT_APP_DETAIL, details)

    key_maps = {
        "genre": dict(con.execute("SELECT steam_genre_id, genre_key FROM genre")),
        "category": dict(con.execute("SELECT steam_category_id, category_key FROM category")),
        "developer": dict(con.execute("SELECT name, developer_key FROM developer")),
        "publisher": dict(con.execute("SELECT name, publisher_key FROM publisher")),
    }
    for app_key, when, app_links in links:
        stored = con.execute("SELECT retrieved_at FROM app_detail WHERE app_key = ?", (app_key,)).fetchone()
        if stored is None or stored[0] != when:
            continue  # the database already holds a newer store page for this app
        for kind, (delete_sql, insert_sql) in BRIDGE_SQL.items():
            con.execute(delete_sql, (app_key,))
            con.executemany(insert_sql, [(app_key, key_maps[kind][k], when) for k in sorted(app_links[kind])])


# =============================================================== app reviews
def load_appreviews(con, files: list, st: Staging) -> None:
    keys = app_keys(con)
    winners = {}  # (steam_appid, snapshot_date) -> ((retrieved_at, rel), raw file, query_summary)
    for raw in files:
        payload = read_json(raw)
        appid = appid_from_ref(raw)
        summary = payload.get("query_summary") if isinstance(payload, dict) else None
        if appid is None:
            st.reject(raw, raw.ref, None, "no_valid_appid", "file name has no appid")
        elif not isinstance(payload, dict) or payload.get("success") not in (1, True):
            st.reject(raw, raw.ref, appid, "unusable_structure", "success is not 1")
        elif not isinstance(summary, dict):
            st.reject(raw, raw.ref, appid, "unusable_structure", "query_summary is missing")
        elif appid not in keys:
            st.reject(raw, raw.ref, appid, "unknown_app", "appid is not in any landed app list")
        else:
            key = (appid, raw.retrieved_at[:10])
            version = (raw.retrieved_at, raw.rel)
            current = winners.get(key)
            if current is not None:
                st.superseded["review_snapshot"] += 1
            if newer(version, current[0] if current else None):
                winners[key] = (version, raw, summary)

    c = st.cleaner
    rows = [
        {
            "app_key": keys[appid],
            "snapshot_date": day,
            "total_positive": c.integer(s.get("total_positive"), "review_snapshot.total_positive", low=0),
            "total_negative": c.integer(s.get("total_negative"), "review_snapshot.total_negative", low=0),
            "total_reviews": c.integer(s.get("total_reviews"), "review_snapshot.total_reviews", low=0),
            "review_score": c.integer(s.get("review_score"), "review_snapshot.review_score", low=0, high=9),
            "review_score_desc": c.text(s.get("review_score_desc"), "review_snapshot.review_score_desc"),
            "retrieved_at": raw.retrieved_at,
        }
        for (appid, day), (_, raw, s) in sorted(winners.items())
    ]
    con.executemany(UPSERT_REVIEW, rows)


# ====================================================================== run
@dataclass
class RunResult:
    ok: bool
    before: dict
    after: dict
    error: str = ""
    report: str = ""


def run(db_path: Path = DB_PATH, raw_dir: Path = RAW_DIR, echo: bool = True) -> RunResult:
    db_path, raw_dir = Path(db_path), Path(raw_dir)
    con = connect(db_path)
    st = Staging()
    files, fk_problems, error = [], None, ""
    try:
        apply_schema(con)
        before = row_counts(con)
        quarantined_before = dict(con.execute("SELECT source_file || '#' || record_ref, 1 FROM quarantine"))
        try:
            with con:                      # one transaction: commit on success, roll back on any error
                con.execute("BEGIN")
                files = list_raw_files(raw_dir)
                by_source = {s: [f for f in files if f.source == s] for s in SOURCES}
                load_applist(con, by_source["applist"], st)
                load_appdetails(con, by_source["appdetails"], st)
                load_appreviews(con, by_source["appreviews"], st)
                con.executemany(UPSERT_QUARANTINE, st.quarantine)
                con.execute(RESOLVE_QUARANTINE, ("unknown_app",))
                fk_problems = con.execute("PRAGMA foreign_key_check").fetchall()
                if fk_problems:
                    raise LoadError(f"foreign key check reported {len(fk_problems)} violation(s)")
        except Exception as exc:          # noqa: BLE001 - reported, and the transaction is rolled back
            error = f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, (LoadError, sqlite3.Error)):
                traceback.print_exc()
        after = row_counts(con)
    finally:
        con.close()

    report = summary(files, before, after, st, quarantined_before, fk_problems, error)
    if echo:
        print(report)
    return RunResult(ok=not error, before=before, after=after, error=error, report=report)


def summary(files, before, after, st, quarantined_before, fk_problems, error) -> str:
    def delta(table):
        d = after[table] - before[table]
        return f"{d:+,}" if d else "+0"

    def entity(label, table):
        return f"{label:<21}{after[table]:,} in database ({after[table] - before[table]:,} new this run)"

    def events(label, table):
        return f"{label:<21}{after[table]:,} in database ({delta(table)} this run)"

    lines = []
    if files:
        stamps = sorted(f.retrieved_at for f in files)
        per_source = " | ".join(f"{s} {sum(f.source == s for f in files)}" for s in SOURCES)
        lines.append(f"{'files read:':<21}{len(files):,} raw files ({stamps[0]} .. {stamps[-1]})")
        lines.append(f"{'':<21}{per_source}")
    else:
        lines.append(f"{'files read:':<21}0 raw files")
    lines += [
        entity("apps:", "app"),
        events("app_details:", "app_detail"),
        entity("genres:", "genre"),
        entity("categories:", "category"),
        entity("developers:", "developer"),
        entity("publishers:", "publisher"),
        f"{'app links:':<21}genre {after['app_genre']:,} ({delta('app_genre')}) | "
        f"category {after['app_category']:,} ({delta('app_category')}) | "
        f"developer {after['app_developer']:,} ({delta('app_developer')}) | "
        f"publisher {after['app_publisher']:,} ({delta('app_publisher')})",
        events("review_snapshots:", "review_snapshot"),
    ]
    superseded = sum(st.superseded.values())
    lines.append(f"{'superseded:':<21}{superseded:,} older retrievals of an already-seen key "
                 "(latest retrieved_at wins)")
    cleaned = st.cleaner.counts
    lines.append(f"{'values cleaned:':<21}{sum(cleaned.values()):,}")
    for what, n in sorted(cleaned.items()):
        lines.append(f"{'':<23}{n:>6,}  {what}")
    new_q = sum(1 for q in st.quarantine
                if f"{q['source_file']}#{q['record_ref']}" not in quarantined_before)
    lines.append(f"{'quarantined:':<21}{len(st.quarantine):,} records this run ({new_q:,} new), "
                 f"{after['quarantine']:,} in quarantine table")
    for (source, reason), n in sorted(Counter((q["source"], q["reason"]) for q in st.quarantine).items()):
        lines.append(f"{'':<23}{n:>6,}  {source}: {reason}")
    if error:
        lines.append(f"{'foreign_key_check:':<21}not reached" if fk_problems is None
                     else f"{'foreign_key_check:':<21}{len(fk_problems)} violation(s)")
        lines.append(f"{'error:':<21}{error}")
        lines.append("STATUS: FAILED - rolled back, nothing from this run was written")
    else:
        lines.append(f"{'foreign_key_check:':<21}OK")
        lines.append("STATUS: COMPLETE")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Load landed Steam raw files into SQLite.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"database file (default: {DB_PATH})")
    parser.add_argument("--raw", type=Path, default=RAW_DIR, help=f"raw data folder (default: {RAW_DIR})")
    args = parser.parse_args()
    return 0 if run(args.db, args.raw).ok else 1


if __name__ == "__main__":
    sys.exit(main())
