# steam-catalog-trends-pipeline
# Steam Catalog Trends by Release Year

## The question

This project looks at how the Steam game catalogue has changed by release year: how many games were released each year and which genres rose or fell. We also want to predict a game's share of positive reviews from its release year, price, genres and platform support. Repeated snapshots of review counts will show how satisfaction and popularity evolve over time. The findings could help a small developer decide which genre and price point to target.

## Data sources

All three come from Steam (Valve Corporation). Full source cards: [docs/sources.md](docs/sources.md).

- **App list** - every app on Steam (`IStoreService/GetAppList`, API key required)
- **App details** - store page data per app (`store.steampowered.com/api/appdetails`)
- **Review summary** - review counts per app (`store.steampowered.com/appreviews`)

Attribution: data provided by Steam / Valve Corporation. <!-- TODO: add any attribution the Steam terms require -->

Raw data is **not committed** (`data/raw/` is git-ignored) until the terms are confirmed to allow redistribution. Run the script to regenerate it.

## How to run

Requires Python 3.9+.

```bash
git clone <repository-url>
cd <repository-folder>

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # Windows: copy .env.example .env
# open .env and set STEAM_API_KEY (free key: https://steamcommunity.com/dev/apikey)

python src/ingest.py
```

Environment variables (all listed in `.env.example`):

| Variable | Meaning | Default |
|---|---|---|
| `STEAM_API_KEY` | Steam Web API key (**required**) | - |
| `MAX_APPS` | Apps fetched per run, `0` = all (many hours) | 300 |
| `SAMPLE_SEED` | Seed for the random sample | 42 |
| `REQUEST_DELAY_SECONDS` | Pause between apps (rate limit ~200 req / 5 min) | 1.6 |
| `REQUEST_TIMEOUT` | Seconds before a request times out | 30 |
| `MAX_RETRIES` | Retries on 429 / 5xx / network errors | 5 |
| `BACKOFF_BASE_SECONDS`, `BACKOFF_MAX_SECONDS` | Exponential backoff settings | 5, 120 |
| `RAW_DATA_DIR` | Output folder | `data/raw` |

Each run saves new files named `<UTC timestamp>_<appid>.json` under `data/raw/applist/`, `data/raw/appdetails/` and `data/raw/appreviews/`. Earlier files are never overwritten. At the end the script prints a run summary (files saved and problems per source). Exit code is `0` if healthy, `1` if there were failures, `2` for authentication or configuration errors.

## Repository structure

```
.
├── README.md
├── .gitignore
├── .env.example        # variable names with placeholder values
├── requirements.txt
├── src/
│   └── ingest.py       # ingestion script
├── data/
│   └── raw/            # landed responses, exactly as received (git-ignored)
└── docs/
    └── sources.md      # data source cards
```

## Status

M1 — ingestion complete

## Team

| Name | GitHub |
|---|---|
| Yasin Gündoğan | @yasingundogan |
| Muhammet Mustafa Sarıbuğa | @se-220717006 |
| Emre Kahraman | @EmreKa-0 |
