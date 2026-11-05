"""Data-quality expectations as code: PASS, WARN or STOP for each, on every run.

Run from the repository root, after python src/clean.py:

    python src/validate.py                    # checks data/steam_catalog.db (opened read-only)
    python src/validate.py --as-of 2026-10-27 # freshness measured against this day instead of today (UTC)
    python src/validate.py --prove-stops      # makes every STOP check fail once, on temporary copies

Levels
- STOP  the cleaned layer must not be used: validate.py exits with 1.
- WARN  worth knowing every time, but the pipeline goes on (exit 0).
- PASS  the expectation holds.

The database is opened read-only. --prove-stops never touches it either: it
copies it to a temporary folder, breaks one thing per STOP check in the copy,
runs this script on the copy and confirms the check says STOP and the exit code
is not 0. Every value reaches SQL through a ? placeholder.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import clean_config as cfg
from db import CLEANS, DB_PATH


@dataclass
class Result:
    level: str      # PASS / WARN / STOP
    name: str
    detail: str


def one(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------ SQL (static)
COUNT_SQL = {table: sql for table, sql in (
    ("app", "SELECT COUNT(*) FROM app"), ("app_clean", "SELECT COUNT(*) FROM app_clean"),
    ("app_detail", "SELECT COUNT(*) FROM app_detail"),
    ("app_detail_clean", "SELECT COUNT(*) FROM app_detail_clean"),
    ("review_snapshot", "SELECT COUNT(*) FROM review_snapshot"),
    ("review_snapshot_clean", "SELECT COUNT(*) FROM review_snapshot_clean"),
    ("developer", "SELECT COUNT(*) FROM developer"), ("developer_clean", "SELECT COUNT(*) FROM developer_clean"),
    ("publisher", "SELECT COUNT(*) FROM publisher"), ("publisher_clean", "SELECT COUNT(*) FROM publisher_clean"),
    ("genre", "SELECT COUNT(*) FROM genre"), ("genre_clean", "SELECT COUNT(*) FROM genre_clean"),
    ("category", "SELECT COUNT(*) FROM category"), ("category_clean", "SELECT COUNT(*) FROM category_clean"),
)}

IMPOSSIBLE_PRICE = """
SELECT COUNT(*) FROM app_detail_clean
WHERE price_initial_cents < 0 OR price_final_cents < 0
   OR price_final_cents > price_initial_cents
   OR discount_percent < 0 OR discount_percent > 100
   OR (price_currency IS NOT NULL AND price_currency <> ?)
   OR abs(price_final_cents - price_initial_cents * (100 - discount_percent) / 100.0) > ?
"""

IMPOSSIBLE_COUNTS = """
SELECT COUNT(*) FROM review_snapshot_clean
WHERE total_positive < 0 OR total_negative < 0 OR total_reviews < 0
   OR total_positive + total_negative <> total_reviews
"""

SCORE_SENTINEL = """
SELECT COUNT(*) FROM review_snapshot_clean
WHERE review_score IS NOT NULL
  AND (review_score NOT BETWEEN 1 AND 9 OR total_reviews_raw < ? OR total_reviews_raw IS NULL)
"""

IMPOSSIBLE_RELEASE = """
SELECT COUNT(*) FROM app_detail_clean
WHERE (release_date IS NOT NULL AND (date(release_date) IS NOT release_date
                                     OR CAST(substr(release_date, 1, 4) AS INTEGER) <> release_year))
   OR (release_date > substr(retrieved_at, 1, 10) AND coming_soon IS NOT 1)
   OR release_year < ?
   OR release_year > CAST(substr(retrieved_at, 1, 4) AS INTEGER) + ?
"""

# Flags and values must agree: a flag that keeps the value keeps it unchanged,
# a flag that removes it leaves NULL (the lab's "flagged, but pm10 still -999").
FLAG_VALUE_DISAGREEMENTS = {
    "app_clean.name": """
        SELECT COUNT(*) FROM app_clean
        WHERE (name_flag = 'ok' AND name IS NOT name_raw)
           OR (name_flag = 'missing_not_provided' AND (name IS NOT NULL OR name_raw IS NOT NULL))
           OR (name_flag = 'text_normalised' AND (name IS name_raw OR name IS NULL))""",
    "app_detail_clean.store_name": """
        SELECT COUNT(*) FROM app_detail_clean
        WHERE (store_name_flag = 'ok' AND store_name IS NOT store_name_raw)
           OR (store_name_flag = 'missing_not_provided' AND store_name IS NOT NULL)
           OR (store_name_flag = 'text_normalised' AND (store_name IS store_name_raw OR store_name IS NULL))""",
    "app_detail_clean.release_date": """
        SELECT COUNT(*) FROM app_detail_clean
        WHERE (release_date_flag IN ('ok', 'announced_future')
               AND (release_date IS NULL OR release_precision <> 'day'))
           OR (release_date_flag IN ('partial_year', 'partial_quarter', 'partial_month')
               AND (release_date IS NOT NULL OR release_year IS NULL))
           OR (release_date_flag LIKE 'missing%' OR release_date_flag LIKE 'invalid%')
               AND (release_date IS NOT NULL OR release_year IS NOT NULL)""",
    "app_detail_clean.price": """
        SELECT COUNT(*) FROM app_detail_clean
        WHERE (price_flag IN ('ok', 'unusual_high')
               AND (price_initial_cents IS NOT price_initial_cents_raw
                    OR price_final_cents IS NOT price_final_cents_raw
                    OR discount_percent IS NOT discount_percent_raw
                    OR price_currency IS NOT price_currency_raw
                    OR price_initial_cents IS NULL))
           OR (price_flag NOT IN ('ok', 'unusual_high')
               AND (price_initial_cents IS NOT NULL OR price_final_cents IS NOT NULL
                    OR discount_percent IS NOT NULL OR price_currency IS NOT NULL))""",
    "app_detail_clean.metacritic_score": """
        SELECT COUNT(*) FROM app_detail_clean
        WHERE (metacritic_score_flag = 'ok' AND (metacritic_score IS NOT metacritic_score_raw
                                                 OR metacritic_score IS NULL))
           OR (metacritic_score_flag <> 'ok' AND metacritic_score IS NOT NULL)""",
    "app_detail_clean.recommendations_total": """
        SELECT COUNT(*) FROM app_detail_clean
        WHERE (recommendations_total_flag = 'ok' AND (recommendations_total IS NOT recommendations_total_raw
                                                      OR recommendations_total IS NULL))
           OR (recommendations_total_flag <> 'ok' AND recommendations_total IS NOT NULL)""",
    "review_snapshot_clean.counts": """
        SELECT COUNT(*) FROM review_snapshot_clean
        WHERE (review_counts_flag IN ('ok', 'zero_reviews', 'unusual_high')
               AND (total_positive IS NOT total_positive_raw OR total_negative IS NOT total_negative_raw
                    OR total_reviews IS NOT total_reviews_raw OR total_reviews IS NULL))
           OR (review_counts_flag = 'zero_reviews' AND total_reviews <> 0)
           OR (review_counts_flag NOT IN ('ok', 'zero_reviews', 'unusual_high')
               AND (total_positive IS NOT NULL OR total_negative IS NOT NULL OR total_reviews IS NOT NULL))""",
    "review_snapshot_clean.score": """
        SELECT COUNT(*) FROM review_snapshot_clean
        WHERE (review_score_flag = 'ok' AND (review_score IS NOT review_score_raw OR review_score IS NULL))
           OR (review_score_flag <> 'ok' AND (review_score IS NOT NULL OR review_score_desc IS NOT NULL))""",
    "developer_clean.name / publisher_clean.name": """
        SELECT (SELECT COUNT(*) FROM developer_clean
                WHERE (name_flag = 'ok' AND name IS NOT name_raw)
                   OR (name_flag <> 'ok' AND name IS name_raw))
             + (SELECT COUNT(*) FROM publisher_clean
                WHERE (name_flag = 'ok' AND name IS NOT name_raw)
                   OR (name_flag <> 'ok' AND name IS name_raw))""",
    "category_clean.description": """
        SELECT COUNT(*) FROM category_clean
        WHERE (description_flag = 'ok' AND description IS NOT description_raw)
           OR (description_flag <> 'ok' AND description IS description_raw)""",
}

YEAR_SCORE_SHARE = """
SELECT d.release_year,
       COUNT(*) AS games,
       SUM(CASE WHEN r.review_score_flag = 'ok' THEN 1 ELSE 0 END) AS usable
FROM app_detail_clean AS d
JOIN review_snapshot_clean AS r ON r.app_key = d.app_key
WHERE d.release_year IS NOT NULL AND d.coming_soon = 0     -- unreleased games cannot have reviews
GROUP BY d.release_year
ORDER BY d.release_year
"""

LATEST_SNAPSHOT = "SELECT MAX(snapshot_date) FROM review_snapshot_clean"
COVERAGE = """
SELECT COUNT(DISTINCT res.resolved_appid),
       COUNT(DISTINCT CASE WHEN r.snapshot_date = ? THEN res.resolved_appid END)
FROM app_resolved AS res
LEFT JOIN review_snapshot_clean AS r ON r.app_key = res.app_key
"""
MISSING_COVERAGE = """
SELECT res.resolved_appid FROM app_resolved AS res
WHERE NOT EXISTS (SELECT 1 FROM review_snapshot_clean AS r
                  WHERE r.app_key = res.app_key AND r.snapshot_date = ?)
ORDER BY 1 LIMIT 5
"""

UNUSUAL_REVIEWS = """
SELECT d.store_name, r.total_reviews FROM review_snapshot_clean AS r
JOIN app_detail_clean AS d ON d.app_key = r.app_key
WHERE r.review_counts_flag = 'unusual_high' ORDER BY r.total_reviews DESC
"""
RECOMMENDATIONS_ABOVE_REVIEWS = """
SELECT COUNT(*), SUM(CASE WHEN d.recommendations_total > r.total_reviews THEN 1 ELSE 0 END)
FROM app_detail_clean AS d JOIN review_snapshot_clean AS r ON r.app_key = d.app_key
WHERE d.recommendations_total IS NOT NULL AND r.total_reviews IS NOT NULL
"""
LABEL_PROBLEMS = """
SELECT (SELECT COUNT(*) FROM category_clean WHERE description LIKE '#%')
     + (SELECT COUNT(*) FROM (SELECT description FROM category_clean
                              GROUP BY description HAVING COUNT(*) > 1))
     + (SELECT COUNT(*) FROM genre_clean WHERE kind_flag = 'unmapped')
"""
COMPANY_NAMES = """
SELECT 'developer', name FROM developer UNION ALL SELECT 'publisher', name FROM publisher
"""
COMPANY_DECISIONS = "SELECT role_a, name_a, role_b, name_b FROM company_match"


# ---------------------------------------------------------------------- checks
def check_row_counts(con) -> list:
    results = []
    for clean_table, raw_table in CLEANS.items():
        n_clean, n_raw = one(con, COUNT_SQL[clean_table]), one(con, COUNT_SQL[raw_table])
        level = "PASS" if n_clean == n_raw else "STOP"
        results.append(Result(level, f"row count {clean_table} = {raw_table}", f"{n_clean:,} = {n_raw:,}"
                              if level == "PASS" else f"{n_clean:,} vs {n_raw:,}"))
    return results


def check_impossible_values(con) -> list:
    price = one(con, IMPOSSIBLE_PRICE, (cfg.EXPECTED_CURRENCY, cfg.PRICE_ROUNDING_TOLERANCE_CENTS))
    counts = one(con, IMPOSSIBLE_COUNTS)
    score = one(con, SCORE_SENTINEL, (cfg.MIN_REVIEWS_FOR_SCORE,))
    release = one(con, IMPOSSIBLE_RELEASE, (cfg.MIN_PLAUSIBLE_RELEASE_YEAR, cfg.MAX_ANNOUNCE_YEARS_AHEAD))
    return [
        Result("PASS" if price == 0 else "STOP", "no impossible cleaned price",
               f"{price} row(s): negative, final > initial, discount outside 0-100, not "
               f"{cfg.EXPECTED_CURRENCY}, or discount off by > {cfg.PRICE_ROUNDING_TOLERANCE_CENTS} cent"),
        Result("PASS" if counts == 0 else "STOP", "no impossible cleaned review counts",
               f"{counts} row(s): negative, or positive + negative <> total"),
        Result("PASS" if score == 0 else "STOP", "no review-score sentinel survives",
               f"{score} score(s) outside 1-9 or with fewer than {cfg.MIN_REVIEWS_FOR_SCORE} reviews"),
        Result("PASS" if release == 0 else "STOP", "no impossible cleaned release date",
               f"{release} row(s): invalid date, future while released, or year outside "
               f"{cfg.MIN_PLAUSIBLE_RELEASE_YEAR}..retrieval + {cfg.MAX_ANNOUNCE_YEARS_AHEAD}"),
    ]


def check_foreign_keys(con) -> list:
    problems = con.execute("PRAGMA foreign_key_check").fetchall()
    tables = sorted({p[0] for p in problems})
    return [Result("PASS" if not problems else "STOP", "foreign_key_check",
                   "OK" if not problems else f"{len(problems)} violation(s) in {', '.join(tables)}")]


def check_flags_agree(con) -> list:
    bad = {name: one(con, sql) for name, sql in FLAG_VALUE_DISAGREEMENTS.items()}
    wrong = {k: v for k, v in bad.items() if v}
    return [Result("PASS" if not wrong else "STOP", "flags agree with cleaned values",
                   f"{len(bad)} value groups checked, 0 disagree" if not wrong
                   else "; ".join(f"{k}: {v}" for k, v in wrong.items()))]


def check_missing_share(con) -> list:
    rows = con.execute(YEAR_SCORE_SHARE).fetchall()
    judged = [(y, n, u) for y, n, u in rows if n >= cfg.MIN_GAMES_FOR_YEAR_CHECK]
    over = [(y, n, u) for y, n, u in judged if (n - u) / n > cfg.MAX_MISSING_SCORE_SHARE]
    detail = (f"{len(judged)} years with >= {cfg.MIN_GAMES_FOR_YEAR_CHECK} released games; over "
              f"{cfg.MAX_MISSING_SCORE_SHARE:.0%} without a usable score: "
              + (", ".join(f"{y} {(n - u) / n:.0%} ({n - u}/{n})" for y, n, u in over) or "none"))
    return [Result("WARN" if over else "PASS", "usable review score per release year", detail)]


def check_coverage_and_freshness(con, as_of: date) -> list:
    latest = one(con, LATEST_SNAPSHOT)
    if latest is None:
        return [Result("WARN", "every sampled app in the latest snapshot", "no snapshots"),
                Result("WARN", f"latest snapshot <= {cfg.MAX_SNAPSHOT_AGE_DAYS} days old", "no snapshots")]
    total, covered = con.execute(COVERAGE, (latest,)).fetchone()
    missing = [str(r[0]) for r in con.execute(MISSING_COVERAGE, (latest,))]
    age = (as_of - date.fromisoformat(latest)).days
    return [
        Result("PASS" if covered == total else "WARN", "every sampled app in the latest snapshot",
               f"{covered} of {total} (resolved) on {latest}" + (f"; missing e.g. {', '.join(missing)}"
                                                                if missing else "")),
        Result("PASS" if age <= cfg.MAX_SNAPSHOT_AGE_DAYS else "WARN",
               f"latest snapshot <= {cfg.MAX_SNAPSHOT_AGE_DAYS} days old",
               f"{latest} is {age} day(s) old on {as_of.isoformat()}"),
    ]


def check_data_specific(con) -> list:
    results = []
    unusual = con.execute(UNUSUAL_REVIEWS).fetchall()
    results.append(Result("PASS" if not unusual else "WARN", "no unusual_high review counts",
                          f"{len(unusual)}: " + ", ".join(f"{n} {t:,}" for n, t in unusual) if unusual
                          else "0 flagged"))
    total, above = con.execute(RECOMMENDATIONS_ABOVE_REVIEWS).fetchone()
    results.append(Result("PASS" if not above else "WARN", "recommendations_total <= total_reviews",
                          f"{above or 0} of {total} apps with both have more recommendations than reviews"))
    labels = one(con, LABEL_PROBLEMS)
    results.append(Result("PASS" if labels == 0 else "WARN", "no placeholder, shared or unmapped labels",
                          f"{labels} problem(s) in category_clean / genre_clean"))
    results.append(check_undecided_names(con))
    return results


def check_undecided_names(con) -> Result:
    from clean import company_proposals, pair_key      # same matching steps as the cleaning run
    proposals = company_proposals(sorted(con.execute(COMPANY_NAMES).fetchall()))
    decided = {pair_key(*row) for row in con.execute(COMPANY_DECISIONS)}
    undecided = sorted(set(proposals) - decided)
    return Result("PASS" if not undecided else "WARN", "every proposed company match has a decision",
                  f"{len(proposals)} proposal(s), {len(undecided)} without a row in data/mappings/company_match.csv")


def run_checks(db_path: Path, as_of: date) -> list:
    con = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return (check_row_counts(con) + check_impossible_values(con) + check_foreign_keys(con)
                + check_flags_agree(con) + check_missing_share(con)
                + check_coverage_and_freshness(con, as_of) + check_data_specific(con))
    finally:
        con.close()


# ------------------------------------------------------------ --prove-stops
# One corruption per STOP check, applied to a temporary copy. Each targets the
# check named on the left and leaves the others passing where possible.
PROVE = (
    ("row count app_detail_clean = app_detail",
     "delete one row of app_detail_clean",
     "DELETE FROM app_detail_clean WHERE app_key = (SELECT MIN(app_key) FROM app_detail_clean)"),
    ("no impossible cleaned price",
     "make one kept final price 1 USD above its initial price (raw and cleaned)",
     """UPDATE app_detail_clean
        SET price_final_cents = price_initial_cents + 100, price_final_cents_raw = price_initial_cents + 100
        WHERE app_key = (SELECT MIN(app_key) FROM app_detail_clean WHERE price_flag = 'ok')"""),
    ("no impossible cleaned review counts",
     "add 1 to one app's positive count (raw and cleaned)",
     """UPDATE review_snapshot_clean
        SET total_positive = total_positive + 1, total_positive_raw = total_positive_raw + 1
        WHERE app_key = (SELECT MIN(app_key) FROM review_snapshot_clean WHERE review_counts_flag = 'ok')"""),
    ("no review-score sentinel survives",
     "keep the raw score 0 of one app with 1-9 reviews and call it ok",
     """UPDATE review_snapshot_clean
        SET review_score = review_score_raw, review_score_flag = 'ok'
        WHERE app_key = (SELECT MIN(app_key) FROM review_snapshot_clean
                         WHERE review_score_flag = 'missing_too_few_reviews')"""),
    ("no impossible cleaned release date",
     "give one released game the date 2099-01-01",
     """UPDATE app_detail_clean SET release_date = '2099-01-01', release_year = 2099
        WHERE app_key = (SELECT MIN(app_key) FROM app_detail_clean
                         WHERE release_date_flag = 'ok' AND coming_soon = 0)"""),
    ("foreign_key_check",
     "point one cleaned review row at an app_key that does not exist (foreign keys off)",
     """UPDATE review_snapshot_clean SET app_key = (SELECT MAX(app_key) + 1000 FROM app)
        WHERE app_key = (SELECT MIN(app_key) FROM review_snapshot_clean)"""),
    ("flags agree with cleaned values",
     "set one cleaned price to NULL while its flag still says ok",
     """UPDATE app_detail_clean SET price_initial_cents = NULL
        WHERE app_key = (SELECT MIN(app_key) FROM app_detail_clean WHERE price_flag = 'ok')"""),
)


def prove_stops(db_path: Path, as_of: date) -> int:
    script = Path(__file__).resolve()
    failures = 0
    with tempfile.TemporaryDirectory(prefix="m3_validate_") as tmp:
        for check, what, sql in PROVE:
            copy = Path(tmp) / "broken.db"
            shutil.copyfile(db_path, copy)
            con = sqlite3.connect(copy)          # plain connection: foreign keys OFF, so we CAN break them
            con.execute(sql)
            con.commit()
            con.close()
            proc = subprocess.run([sys.executable, str(script), "--db", str(copy), "--as-of", as_of.isoformat()],
                                  capture_output=True, text=True)
            said_stop = any(line.startswith("STOP") and check in line for line in proc.stdout.splitlines())
            ok = said_stop and proc.returncode != 0
            failures += not ok
            print(f"{'OK  ' if ok else 'FAIL'} {check}: {what} -> "
                  f"{'STOP' if said_stop else 'no STOP'}, exit {proc.returncode}")
            copy.unlink()
    print(f"\n{len(PROVE) - failures}/{len(PROVE)} STOP checks failed when they should")
    return 0 if failures == 0 else 1


# ------------------------------------------------------------------------ main
def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Data-quality checks on the cleaned layer.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"database (default: {DB_PATH})")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="day freshness is measured against (default: today, UTC)")
    parser.add_argument("--prove-stops", action="store_true",
                        help="make each STOP check fail once on a temporary copy")
    args = parser.parse_args()
    as_of = args.as_of or datetime.now(timezone.utc).date()
    if not args.db.is_file():
        print(f"{args.db} does not exist - run python src/load.py and python src/clean.py first")
        return 2
    if args.prove_stops:
        return prove_stops(args.db, as_of)

    try:
        results = run_checks(args.db, as_of)
    except sqlite3.Error as exc:
        print(f"STOP  cleaned layer readable                     {exc} - run python src/clean.py")
        return 1
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.level:<5} {r.name:<{width}}  {r.detail}")
    levels = [r.level for r in results]
    print(f"\n{len(results)} checks: {levels.count('PASS')} PASS · {levels.count('WARN')} WARN · "
          f"{levels.count('STOP')} STOP")
    print("STATUS: STOP - do not use the cleaned layer" if "STOP" in levels else "STATUS: OK")
    return 1 if "STOP" in levels else 0


if __name__ == "__main__":
    sys.exit(main())
