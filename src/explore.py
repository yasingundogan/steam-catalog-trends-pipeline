"""Helpers for notebooks/m4_eda.ipynb: read-only connection, saved queries,
figure saving and a game-level bootstrap. Standard library + pandas/numpy/matplotlib."""

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from db import DB_PATH, REPO_ROOT

QUERIES_DIR = REPO_ROOT / "sql" / "queries"
FIGURES_DIR = REPO_ROOT / "figures"


def connect_ro(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the database read-only: any write raises sqlite3.OperationalError."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"{db_path} not found - run python src/load.py and python src/clean.py first")
    return sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True)


def query(con: sqlite3.Connection, name: str, **params) -> pd.DataFrame:
    """Run sql/queries/<name>.sql with named parameters; lists are bound as JSON arrays."""
    sql = (QUERIES_DIR / f"{name}.sql").read_text(encoding="utf-8")
    bound = {k: json.dumps(v) if isinstance(v, (list, tuple)) else v for k, v in params.items()}
    return pd.read_sql_query(sql, con, params=bound)


def save(fig, name: str, caption: str) -> Path:
    """Write the caption (n, flag filter, clock) under the figure and save it to figures/."""
    FIGURES_DIR.mkdir(exist_ok=True)
    fig.text(0.01, 0.005, caption, ha="left", va="bottom", fontsize=7.5, color="#444444", wrap=True)
    path = FIGURES_DIR / f"{name}.png"
    # fixed metadata so re-running on the same data writes byte-identical files
    fig.savefig(path, dpi=130, metadata={"Software": None})
    return path


def median_diff_ci(a, b, n_boot: int, seed: int, level: float = 0.95):
    """Median(a) - median(b) and a percentile bootstrap interval, resampling games
    within each group (games are the independent unit: one row per game)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    rng = np.random.default_rng(seed)
    diffs = (np.median(rng.choice(a, (n_boot, a.size)), axis=1)
             - np.median(rng.choice(b, (n_boot, b.size)), axis=1))
    lo, hi = np.quantile(diffs, [(1 - level) / 2, (1 + level) / 2])
    return float(np.median(a) - np.median(b)), float(lo), float(hi)


if __name__ == "__main__":
    # self-check: identical groups give 0 inside the interval; shifted groups give the shift
    d, lo, hi = median_diff_ci([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], 2000, 1)
    assert d == 0 and lo <= 0 <= hi
    d, *_ = median_diff_ci([11, 12, 13], [1, 2, 3], 2000, 1)
    assert d == 10
    print("explore.py self-check OK")
