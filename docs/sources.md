# Data source cards

Three endpoints of the same provider (Valve / Steam). Fields marked **TODO** must be filled in by the team before tagging `m1`.

---

## 1. Steam app list

```
source_name: Steam app list (IStoreService/GetAppList)
provider: Valve Corporation (Steamworks Web API)
url: https://api.steampowered.com/IStoreService/GetAppList/v1/
     docs: https://partner.steamgames.com/doc/webapi
access_method: REST API (JSON), free Web API key required
licence: none stated   # TODO: confirm in the Steam Web API Terms of Use
terms_notes: TODO - read https://steamcommunity.com/dev/apiterms and summarise what it says
             about storing, redistributing and attributing the data. Until confirmed,
             raw data is NOT committed (data/raw/ is in .gitignore).
update_cadence: continuously (new apps are added daily)
coverage: all apps in the Steam catalogue at retrieval time; paginated with
          last_appid / have_more_results
record_meaning: one row = one Steam app (appid, name, ...) - includes non-game items
join_key: appid (joins to: appdetails, appreviews)
first_retrieved: TODO - UTC timestamp of the first file in data/raw/applist/
known_issues: |
  - Not every appid is a game (software, DLC, demos, videos may appear).
  - Large response (tens of thousands of apps per page); the key must never be logged or committed.
```

## 2. Steam store app details

```
source_name: Steam Store - app details
provider: Valve Corporation (Steam Store, unofficial/undocumented API)
url: https://store.steampowered.com/api/appdetails?appids=<appid>
access_method: HTTP/JSON, no key required
licence: none stated   # TODO: confirm
terms_notes: TODO - see the Steam terms linked in card 1. Raw data not committed until confirmed.
update_cadence: daily
coverage: one request per appid; this project uses a reproducible random sample
          (MAX_APPS, SAMPLE_SEED), requested with cc=us, l=english
record_meaning: one response = store page details of one app (name, type, release date,
                price, genres, platforms, ...)
join_key: appid (joins to: appreviews, app list)
first_retrieved: TODO - UTC timestamp of the first file in data/raw/appdetails/
known_issues: |
  - Rate limit of about 200 requests per 5 minutes per IP; the script throttles
    (REQUEST_DELAY_SECONDS) and backs off on 429/5xx.
  - Some appids return "success": false (no store page); these are saved and reported as "unusable".
  - Some ids are not games (type is not "game").
  - release_date is free text (sometimes just "Coming soon").
  - Text fields contain HTML remnants.
  - Fields such as Metacritic score are mostly missing.
  - A 403 from the store endpoint can mean the IP was temporarily blocked.
```

## 3. Steam store review summary

```
source_name: Steam Store - review summary
provider: Valve Corporation (Steam Store, undocumented endpoint)
url: https://store.steampowered.com/appreviews/<appid>?json=1&num_per_page=0
access_method: HTTP/JSON, no key required
licence: none stated   # TODO: confirm
terms_notes: TODO - see the Steam terms linked in card 1. Raw data not committed until confirmed.
update_cadence: continuously (review counts change over time)
coverage: one request per sampled appid, language=all, purchase_type=all;
          num_per_page=0 returns only the summary (query_summary)
record_meaning: one response = review summary of one app (positive / negative / total
                reviews, review score and description)
join_key: appid (joins to: appdetails, app list)
first_retrieved: TODO - UTC timestamp of the first file in data/raw/appreviews/
known_issues: |
  - The endpoint is undocumented, so its structure may change without notice; the script
    reports responses without "success" or "query_summary" as unusable.
  - Review counts are a snapshot; repeated runs are needed to see change over time.
```
