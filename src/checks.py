"""Prove it, don't claim it: six checks against a TEMPORARY COPY of the database.

Run from the repository root, after python src/load.py:

    python src/checks.py

The real database (data/steam_catalog.db) is only ever copied, never opened
for writing. Every check prints PASS or FAIL with the evidence; the exit code
is 0 only if all six pass.

  1. Duplicate natural key  inserting an entity with an existing source id is rejected
  2. Orphan event           inserting an event / link to a non-existent entity is rejected
  3. NULL key               inserting an event with a NULL in its key is rejected
  4. Wrong type             inserting text into a numeric column is rejected
  5. Idempotence            loading twice gives identical row counts (and contents) in every table
  6. Atomicity              a load that fails partway leaves every table unchanged

Each rejection test also runs a control insert that differs only in the
property under test and must be ACCEPTED - so a PASS cannot come from some
other constraint failing by accident.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import load
from db import DB_PATH, RAW_DIR, connect, row_counts, table_fingerprints

TS = "2099-01-01T00:00:00Z"   # a valid ISO 8601 UTC timestamp for probe rows
DAY = "2099-01-01"            # snapshot_date that no real row uses


# ------------------------------------------------------------------ helpers
def attempt(con: sqlite3.Connection, sql: str, params: tuple):
    """Run one statement, then roll back. Returns (accepted, message)."""
    try:
        con.execute(sql, params)
        return True, "accepted"
    except sqlite3.IntegrityError as exc:
        return False, str(exc)
    finally:
        con.rollback()


def must_reject(con, label, sql, params, expect_in_error):
    accepted, msg = attempt(con, sql, params)
    ok = not accepted and expect_in_error.lower() in msg.lower()
    return ok, f"{label}: {'rejected' if not accepted else 'ACCEPTED'} ({msg})"


def must_accept(con, label, sql, params):
    accepted, msg = attempt(con, sql, params)
    return accepted, f"control {label}: {msg}"


def one(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


def run_group(results):
    """results: list of (ok, evidence). Passes only if every item passed."""
    return all(ok for ok, _ in results), [evidence for _, evidence in results]


# ------------------------------------------------------------- checks 1 - 4
def check_duplicate_key(con):
    appid = one(con, "SELECT steam_appid FROM app ORDER BY app_key LIMIT 1")
    new_appid = one(con, "SELECT MAX(steam_appid) + 1 FROM app")
    genre_id = one(con, "SELECT steam_genre_id FROM genre ORDER BY genre_key LIMIT 1")
    dev_name = one(con, "SELECT name FROM developer ORDER BY developer_key LIMIT 1")
    if appid is None or genre_id is None or dev_name is None:
        return False, ["tables are empty - run python src/load.py first"]
    insert_app = "INSERT INTO app (steam_appid, name, retrieved_at) VALUES (?, ?, ?)"
    insert_genre = "INSERT INTO genre (steam_genre_id, description, retrieved_at) VALUES (?, ?, ?)"
    insert_dev = "INSERT INTO developer (name, retrieved_at) VALUES (?, ?)"
    return run_group([
        must_accept(con, "new app.steam_appid", insert_app, (new_appid, "probe", TS)),
        must_reject(con, f"existing app.steam_appid {appid}", insert_app, (appid, "probe", TS), "UNIQUE"),
        must_reject(con, f"existing genre.steam_genre_id {genre_id}", insert_genre,
                    (genre_id, "probe", TS), "UNIQUE"),
        must_reject(con, "existing developer.name", insert_dev, (dev_name, TS), "UNIQUE"),
    ])


def check_orphan_event(con):
    app_key = one(con, "SELECT app_key FROM app_detail ORDER BY app_key LIMIT 1")
    missing_app = one(con, "SELECT MAX(app_key) + 1000 FROM app")
    missing_genre = one(con, "SELECT COALESCE(MAX(genre_key), 0) + 1000 FROM genre")
    if app_key is None:
        return False, ["app_detail is empty - run python src/load.py first"]
    insert_review = ("INSERT INTO review_snapshot (app_key, snapshot_date, total_positive, retrieved_at) "
                     "VALUES (?, ?, ?, ?)")
    insert_link = "INSERT INTO app_genre (app_key, genre_key, retrieved_at) VALUES (?, ?, ?)"
    return run_group([
        must_accept(con, "review_snapshot for existing app", insert_review, (app_key, DAY, 1, TS)),
        must_reject(con, f"review_snapshot for non-existent app_key {missing_app}", insert_review,
                    (missing_app, DAY, 1, TS), "FOREIGN KEY"),
        must_reject(con, f"app_genre to non-existent genre_key {missing_genre}", insert_link,
                    (app_key, missing_genre, TS), "FOREIGN KEY"),
    ])


def check_null_key(con):
    app_key = one(con, "SELECT app_key FROM app ORDER BY app_key LIMIT 1")
    if app_key is None:
        return False, ["app is empty - run python src/load.py first"]
    insert_review = ("INSERT INTO review_snapshot (app_key, snapshot_date, total_positive, retrieved_at) "
                     "VALUES (?, ?, ?, ?)")
    insert_app = "INSERT INTO app (steam_appid, name, retrieved_at) VALUES (?, ?, ?)"
    return run_group([
        must_accept(con, "review_snapshot with full key", insert_review, (app_key, DAY, 1, TS)),
        must_reject(con, "review_snapshot with app_key NULL", insert_review, (None, DAY, 1, TS), "NOT NULL"),
        must_reject(con, "review_snapshot with snapshot_date NULL", insert_review,
                    (app_key, None, 1, TS), "NOT NULL"),
        must_reject(con, "app with steam_appid NULL", insert_app, (None, "probe", TS), "NOT NULL"),
    ])


def check_wrong_type(con):
    app_key = one(con, "SELECT app_key FROM app_detail ORDER BY app_key LIMIT 1")
    if app_key is None:
        return False, ["app_detail is empty - run python src/load.py first"]
    insert_review = ("INSERT INTO review_snapshot (app_key, snapshot_date, total_positive, retrieved_at) "
                     "VALUES (?, ?, ?, ?)")
    update_price = "UPDATE app_detail SET price_final_cents = ? WHERE app_key = ?"
    return run_group([
        must_accept(con, "integer total_positive", insert_review, (app_key, DAY, 12, TS)),
        must_reject(con, "text 'lots' into review_snapshot.total_positive", insert_review,
                    (app_key, DAY, "lots", TS), "cannot store TEXT"),
        must_reject(con, "text 'n/a' into app_detail.price_final_cents", update_price,
                    ("n/a", app_key), "cannot store TEXT"),
    ])


# -------------------------------------------------------------- checks 5 - 6
def check_idempotence(db_copy: Path, raw_dir: Path):
    first = load.run(db_copy, raw_dir, echo=False)
    con = connect(db_copy)
    try:
        counts_1, prints_1 = row_counts(con), table_fingerprints(con)
    finally:
        con.close()
    second = load.run(db_copy, raw_dir, echo=False)
    con = connect(db_copy)
    try:
        counts_2, prints_2 = row_counts(con), table_fingerprints(con)
    finally:
        con.close()
    if not (first.ok and second.ok):
        return False, [f"a load failed: {first.error or second.error}"]
    changed = [t for t in counts_1 if counts_1[t] != counts_2[t] or prints_1[t] != prints_2[t]]
    evidence = [f"run 1 -> run 2 row counts: "
                + ", ".join(f"{t} {counts_1[t]:,}->{counts_2[t]:,}" for t in counts_1)]
    if changed:
        evidence.append(f"tables that changed on the second run: {changed}")
    else:
        evidence.append(f"contents identical in all {len(counts_1)} tables (sha256 of every row)")
    return not changed, evidence


def check_atomicity(db_copy: Path, raw_dir: Path, work: Path):
    # A copy of the raw data with two extra files, both retrieved "later" than everything else:
    #  - an app-list page with one NEW app (written early in the load if it were not atomic)
    #  - a damaged review response (read late in the load -> the run fails partway)
    raw_copy = work / "raw_broken"
    shutil.copytree(raw_dir, raw_copy)
    probe_appid = 999_999_999
    (raw_copy / "applist").mkdir(parents=True, exist_ok=True)
    (raw_copy / "appreviews").mkdir(parents=True, exist_ok=True)
    (raw_copy / "applist" / "20991231T235958Z_p001.json").write_text(json.dumps(
        {"response": {"apps": [{"appid": probe_appid, "name": "atomicity probe",
                                "last_modified": 1790000000, "price_change_number": 0}]}}),
        encoding="utf-8")
    (raw_copy / "appreviews" / f"20991231T235959Z_{probe_appid}.json").write_text(
        '{"success":1,"query_summary":{"num_reviews":0,"total_positive":', encoding="utf-8")

    con = connect(db_copy)
    try:
        counts_before, prints_before = row_counts(con), table_fingerprints(con)
    finally:
        con.close()
    result = load.run(db_copy, raw_copy, echo=False)
    con = connect(db_copy)
    try:
        counts_after, prints_after = row_counts(con), table_fingerprints(con)
        probe_present = one(con, "SELECT COUNT(*) FROM app WHERE steam_appid = ?", (probe_appid,))
    finally:
        con.close()

    evidence = [f"load with one damaged record: {'FAILED as expected' if not result.ok else 'did NOT fail'}"
                + (f" ({result.error})" if result.error else "")]
    unchanged = counts_before == counts_after and prints_before == prints_after
    evidence.append("row counts and contents unchanged in every table" if unchanged
                    else f"tables changed: {[t for t in counts_before if prints_before[t] != prints_after[t]]}")
    evidence.append(f"probe app written before the failure is present: {'yes' if probe_present else 'no'}")
    return (not result.ok) and unchanged and not probe_present, evidence


# --------------------------------------------------------------------- main
def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Run the six M2 checks on a temporary copy of the database.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"database to copy (default: {DB_PATH})")
    parser.add_argument("--raw", type=Path, default=RAW_DIR, help=f"raw data folder (default: {RAW_DIR})")
    args = parser.parse_args()
    if not args.db.is_file():
        print(f"{args.db} does not exist - build it first with: python src/load.py")
        return 2

    print(f"checks run against a temporary copy of {args.db}")
    results = []
    with tempfile.TemporaryDirectory(prefix="m2_checks_") as tmp:
        work = Path(tmp)

        constraints_db = work / "constraints.db"
        shutil.copyfile(args.db, constraints_db)
        con = connect(constraints_db)
        try:
            for name, check in (("Duplicate natural key", check_duplicate_key),
                                ("Orphan event", check_orphan_event),
                                ("NULL key", check_null_key),
                                ("Wrong type", check_wrong_type)):
                results.append((name, *check(con)))
        finally:
            con.close()

        idem_db = work / "idempotence.db"
        shutil.copyfile(args.db, idem_db)
        results.append(("Idempotence", *check_idempotence(idem_db, args.raw)))

        atomic_db = work / "atomicity.db"
        shutil.copyfile(args.db, atomic_db)
        results.append(("Atomicity", *check_atomicity(atomic_db, args.raw, work)))

    for name, ok, evidence in results:
        print(f"\n[{'PASS' if ok else 'FAIL'}] {name}")
        for line in evidence:
            print(f"       - {line}")
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
