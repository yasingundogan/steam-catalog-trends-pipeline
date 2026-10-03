

from __future__ import annotations

import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")  # does not override variables already set in the shell

log = logging.getLogger("ingest")


# --------------------------------------------------------------------------- errors
class ConfigError(Exception):
    """Missing or invalid configuration."""


class AuthError(Exception):
    """HTTP 401/403: stop immediately, never retry."""


class RequestFailed(Exception):
    """A request gave up (retries exhausted or a non-retryable status)."""


class FatalSourceError(Exception):
    """A source we cannot continue without (the app list) returned something unusable."""


# ------------------------------------------------------------------------- config
@dataclass
class Config:
    api_key: str
    applist_url: str
    appdetails_url: str
    appreviews_url: str
    raw_dir: Path
    timeout: float
    max_retries: int
    backoff_base: float
    backoff_max: float
    delay: float
    max_apps: int
    seed: int
    page_size: int


def _number(name: str, default, cast):
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {raw!r}")
    return value


def load_config() -> Config:
    api_key = os.environ.get("STEAM_API_KEY", "").strip()
    if not api_key or api_key == "your-key-here":
        raise ConfigError(
            "STEAM_API_KEY is not set. Copy .env.example to .env and put your key in it "
            "(see README), or export the variable in your shell."
        )
    raw_dir = Path(os.environ.get("RAW_DATA_DIR", "data/raw"))
    if not raw_dir.is_absolute():
        raw_dir = REPO_ROOT / raw_dir
    return Config(
        api_key=api_key,
        applist_url=os.environ.get(
            "STEAM_APPLIST_URL", "https://api.steampowered.com/IStoreService/GetAppList/v1/"
        ),
        appdetails_url=os.environ.get(
            "STEAM_APPDETAILS_URL", "https://store.steampowered.com/api/appdetails"
        ),
        appreviews_url=os.environ.get(
            "STEAM_REVIEWS_URL", "https://store.steampowered.com/appreviews"
        ).rstrip("/"),
        raw_dir=raw_dir,
        timeout=_number("REQUEST_TIMEOUT", 30, float),
        max_retries=_number("MAX_RETRIES", 5, int),
        backoff_base=_number("BACKOFF_BASE_SECONDS", 5, float),
        backoff_max=_number("BACKOFF_MAX_SECONDS", 120, float),
        delay=_number("REQUEST_DELAY_SECONDS", 1.6, float),
        max_apps=_number("MAX_APPS", 300, int),
        seed=_number("SAMPLE_SEED", 42, int),
        page_size=_number("APPLIST_PAGE_SIZE", 50000, int),
    )


# -------------------------------------------------------------------------- stats
@dataclass
class SourceStats:
    name: str
    requested: int = 0   # logical requests (apps for store sources, pages for the app list)
    saved: int = 0       # raw files written
    ok: int = 0          # saved and passed the sanity check
    retries: int = 0
    unusable: list = field(default_factory=list)  # saved, but empty / unexpected content
    failed: list = field(default_factory=list)    # nothing saved


# ---------------------------------------------------------------------- raw files
def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def save_raw(directory: Path, stem: str, ext: str, content: bytes) -> Path:
    """Write bytes exactly as received. Never overwrites: 'xb' fails if the file exists."""
    directory.mkdir(parents=True, exist_ok=True)
    suffix = 0
    while True:
        name = f"{stem}.{ext}" if suffix == 0 else f"{stem}_{suffix}.{ext}"
        path = directory / name
        try:
            with open(path, "xb") as fh:
                fh.write(content)
            return path
        except FileExistsError:
            suffix += 1


# ------------------------------------------------------------------------ HTTP
class Fetcher:
    """GET with timeout, bounded retries and exponential backoff."""

    def __init__(self, cfg: Config, session: requests.Session):
        self.cfg = cfg
        self.session = session

    def _wait_time(self, attempt: int, retry_after) -> float:
        delay = min(self.cfg.backoff_max, self.cfg.backoff_base * 2 ** (attempt - 1))
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.cfg.backoff_max))
        return delay + random.uniform(0, 1)  # small jitter

    def get(self, url: str, params: dict, stats: SourceStats, ctx: str) -> requests.Response:
        attempts = self.cfg.max_retries + 1
        for attempt in range(1, attempts + 1):
            retry_after = None
            try:
                resp = self.session.get(url, params=params, timeout=self.cfg.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                # Log only the exception type: the message can contain the full URL (and key).
                reason = type(exc).__name__
            else:
                code = resp.status_code
                if code in (401, 403):
                    raise AuthError(
                        f"HTTP {code} from {stats.name} ({ctx}). Check STEAM_API_KEY. "
                        "For the store endpoints a 403 can also mean your IP is blocked for "
                        "sending too many requests: wait a while and raise REQUEST_DELAY_SECONDS. "
                        "Not retrying."
                    )
                if code == 200:
                    return resp
                if code == 429 or code >= 500:
                    reason = f"HTTP {code}"
                    try:
                        retry_after = float(resp.headers.get("Retry-After", ""))
                    except ValueError:
                        retry_after = None
                else:
                    raise RequestFailed(f"HTTP {code} (not retried)")

            if attempt == attempts:
                raise RequestFailed(f"{reason}; gave up after {attempts} attempts")
            wait = self._wait_time(attempt, retry_after)
            stats.retries += 1
            log.warning("%s: %s, retry %d/%d in %.1fs", ctx, reason, attempt, self.cfg.max_retries, wait)
            time.sleep(wait)
        raise RequestFailed("no attempts made")  # not reachable; keeps type checkers happy


# ------------------------------------------------------------------- source: app list
def ingest_applist(fetcher: Fetcher, cfg: Config, stats: SourceStats) -> list:
    """Page through GetAppList, saving every page raw. Returns the list of app dicts."""
    apps: list = []
    last_appid = 0
    page = 0
    while True:
        page += 1
        stats.requested += 1
        params = {"key": cfg.api_key, "max_results": cfg.page_size, "last_appid": last_appid}
        try:
            resp = fetcher.get(cfg.applist_url, params, stats, f"applist page {page}")
        except RequestFailed as exc:
            raise FatalSourceError(f"app list page {page} failed: {exc}") from None
        if not resp.content.strip():
            raise FatalSourceError(f"app list page {page} returned an empty body")

        save_raw(cfg.raw_dir / "applist", f"{utc_stamp()}_p{page:03d}", "json", resp.content)
        stats.saved += 1

        # Parsing happens only after the raw response is on disk.
        try:
            payload = resp.json()
        except ValueError:
            raise FatalSourceError(f"app list page {page} is not valid JSON") from None
        body = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            raise FatalSourceError(f"app list page {page} has an unexpected structure")
        page_apps = body.get("apps") or []
        if page == 1 and not page_apps:
            raise FatalSourceError("app list is empty on the first page")
        stats.ok += 1
        apps.extend(page_apps)
        log.info("applist page %d: %d apps (total %d)", page, len(page_apps), len(apps))

        if not body.get("have_more_results"):
            break
        next_id = body.get("last_appid")
        if not isinstance(next_id, int) or next_id <= last_appid:
            raise FatalSourceError(f"app list pagination is stuck at last_appid={next_id!r}")
        last_appid = next_id
    return apps


def choose_sample(apps: list, cfg: Config) -> list:
    """Reproducible random sample (same SAMPLE_SEED + same list -> same apps). 0 = all."""
    usable = sorted((a for a in apps if isinstance(a, dict) and "appid" in a), key=lambda a: a["appid"])
    if cfg.max_apps == 0 or cfg.max_apps >= len(usable):
        return usable
    return random.Random(cfg.seed).sample(usable, cfg.max_apps)


# ----------------------------------------------------- sources: appdetails / appreviews
def validate_details(payload, appid) -> str | None:
    entry = payload.get(str(appid)) if isinstance(payload, dict) else None
    if not isinstance(entry, dict):
        return "unexpected structure (no entry for this appid)"
    if not entry.get("success"):
        return "success=false (no store data for this appid)"
    if not isinstance(entry.get("data"), dict) or not entry["data"]:
        return "success=true but data is empty"
    return None


def validate_reviews(payload, appid) -> str | None:
    if not isinstance(payload, dict) or not payload.get("success"):
        return "success is not 1 (unexpected response)"
    if not isinstance(payload.get("query_summary"), dict):
        return "query_summary is missing (response format may have changed)"
    return None


def fetch_and_land(fetcher, url, params, directory, appid, stats, validate) -> str:
    """Fetch one response, save it raw, then check it. Returns ok / unusable / failed."""
    stats.requested += 1
    try:
        resp = fetcher.get(url, params, stats, f"{stats.name} {appid}")
    except RequestFailed as exc:
        stats.failed.append((appid, str(exc)))
        return "failed"
    if not resp.content.strip():
        stats.failed.append((appid, "empty response body (nothing saved)"))
        return "failed"

    save_raw(directory, f"{utc_stamp()}_{appid}", "json", resp.content)
    stats.saved += 1

    try:
        payload = resp.json()
    except ValueError:
        stats.unusable.append((appid, "response is not valid JSON"))
        return "unusable"
    problem = validate(payload, appid)
    if problem:
        stats.unusable.append((appid, problem))
        return "unusable"
    stats.ok += 1
    return "ok"


def ingest_apps(fetcher: Fetcher, cfg: Config, sample: list, details: SourceStats, reviews: SourceStats):
    total = len(sample)
    for i, app in enumerate(sample, 1):
        appid = app["appid"]
        d = fetch_and_land(
            fetcher, cfg.appdetails_url, {"appids": appid, "cc": "us", "l": "english"},
            cfg.raw_dir / "appdetails", appid, details, validate_details,
        )
        r = fetch_and_land(
            fetcher, f"{cfg.appreviews_url}/{appid}",
            {"json": 1, "num_per_page": 0, "language": "all", "purchase_type": "all"},
            cfg.raw_dir / "appreviews", appid, reviews, validate_reviews,
        )
        log.info("[%d/%d] appid=%s details=%s reviews=%s", i, total, appid, d, r)
        time.sleep(cfg.delay)


# ----------------------------------------------------------------------- summary
def print_summary(stats_list, started, outcome, total_apps, sample_size, cfg):
    finished = utc_stamp()
    line = "=" * 72
    print(f"\n{line}\nRUN SUMMARY   started {started}   finished {finished}")
    print(f"raw directory: {cfg.raw_dir}")
    print(f"apps in list: {total_apps}   apps sampled: {sample_size} (MAX_APPS={cfg.max_apps}, SAMPLE_SEED={cfg.seed})")
    print(f"\n{'source':<12}{'requests':>9}{'saved':>7}{'ok':>7}{'unusable':>10}{'failed':>8}{'retries':>9}")
    for s in stats_list:
        print(f"{s.name:<12}{s.requested:>9}{s.saved:>7}{s.ok:>7}{len(s.unusable):>10}{len(s.failed):>8}{s.retries:>9}")
    print("\n'saved' = raw files written. 'unusable' = saved, but empty/unexpected content "
          "(e.g. success=false for non-game ids).\n'failed' = nothing saved.")
    for s in stats_list:
        for label, items in (("FAILED", s.failed), ("UNUSABLE", s.unusable)):
            if items:
                print(f"\n{s.name} {label} (first 10 of {len(items)}):")
                for appid, why in items[:10]:
                    print(f"  appid {appid}: {why}")
    print(f"\nRESULT: {outcome}\n{line}")


# -------------------------------------------------------------------------- main
def main() -> int:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    started = utc_stamp()
    applist = SourceStats("applist")
    details = SourceStats("appdetails")
    reviews = SourceStats("appreviews")
    total_apps = sample_size = 0
    outcome, code = "HEALTHY - all sources fetched, no failures", 0

    try:
        with requests.Session() as session:
            fetcher = Fetcher(cfg, session)
            apps = ingest_applist(fetcher, cfg, applist)
            total_apps = len(apps)
            sample = choose_sample(apps, cfg)
            sample_size = len(sample)
            log.info("sampled %d of %d apps", sample_size, total_apps)
            ingest_apps(fetcher, cfg, sample, details, reviews)
        if details.failed or reviews.failed:
            outcome, code = "COMPLETED WITH FAILURES - see failed lists above", 1
        elif sample_size == 0:
            outcome, code = "PROBLEM - nothing was sampled", 1
    except AuthError as exc:
        outcome, code = f"ABORTED - authentication failure: {exc}", 2
    except FatalSourceError as exc:
        outcome, code = f"ABORTED - {exc}", 1
    except KeyboardInterrupt:
        outcome, code = "INTERRUPTED by user - partial run, files saved so far are kept", 130

    print_summary([applist, details, reviews], started, outcome, total_apps, sample_size, cfg)
    return code


if __name__ == "__main__":
    sys.exit(main())
