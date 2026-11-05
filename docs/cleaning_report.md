# Cleaning Report — Steam Catalog Trends by Release Year

Milestone M3 · Lifecycle stage: CLEAN · Data: one landing run, 2026-10-03 (190,242 catalogue apps, 100 sampled games)
Profile notebook: [`notebooks/m3_profile.ipynb`](../notebooks/m3_profile.ipynb) · Code: [`src/clean.py`](../src/clean.py),
[`src/clean_config.py`](../src/clean_config.py), [`src/validate.py`](../src/validate.py) · Schema:
[`sql/clean_schema.sql`](../sql/clean_schema.sql) · Decisions: [`data/mappings/`](../data/mappings/)

## 1. Summary

We profiled the M2 database built from the Steam app list (190,242 apps) and the store pages and review
summaries of a random sample of 100 games. The most important problem is a sentinel: `review_score = 0`
does not mean "worst", it means "fewer than 10 reviews, no score shown", and it covers 58 of the 100
games. Other findings: 27 games have no full release date (all unreleased), two category ids share one label
each and a third arrives as an untranslated localisation key, two genres are not genres, two publisher names
are misspellings of a developer on the same app, and three games have unusually many reviews (kept). The
cleaned layer is seven STRICT tables with the grain, key and row count of the M2 tables they clean, a raw
value, a cleaned value and a flag for everything cleaning can change, and four mapping tables loaded from
committed CSVs; nothing is imputed and nothing is deleted. What we still cannot say: anything about years
with one to three games, anything about positive review share that is not limited to games with at least
10 reviews, and anything about change over time, because there is only one snapshot.

**Changes to M2 since the `m2` tag.**
- `.gitignore` listed `_pycache_/` instead of `__pycache__/`, so compiled Python files were not ignored. Fixed.
- `src/db.py` refuses to count or fingerprint tables it does not know (`known_tables()`), so `load.py` and
  `checks.py` would have crashed once the cleaned tables existed. The new tables are registered there
  (`MAPPING_TABLES`, `CLEAN_TABLES`, `CLEANS`), and `table_fingerprints()` can be limited to the M2 tables.
  `python src/checks.py` still passes 6/6 with the cleaned layer present; the loader and schema are unchanged.
- `requirements.txt` adds pandas, matplotlib and Jupyter for the notebook only; `load.py`, `clean.py` and
  `validate.py` still use only the standard library.

## 2. Profile highlights

All numbers below come from the notebook, run on a fresh build of `data/steam_catalog.db`.

**Coverage per release year** (notebook §1). One retrieval day; 1-16 games per year.

| Year | 2013 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | no full date |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Games | 1 | 1 | 3 | 4 | 7 | 3 | 5 | 6 | 9 | 1 | 10 | 7 | 16 | 27 |

**Missing values against the fields that explain them** (notebook §2).

| Column | Missing | Explained by |
|---|---|---|
| `release_date` | 27 / 100 | all 27 have `coming_soon = 1`; 0 released games lack a date |
| price (`price_overview`) | 40 / 100 | 12 free (`is_free = 1`), 28 unreleased paid; 0 released paid games lack a price |
| `metacritic_score` | 99 / 100 | only ELEX has one |
| `recommendations_total` | 91 / 100 | present for 9 paid apps with 149-19,627 reviews; absent for every free game |

**The sentinel** (notebook §3): review score against review count.

| `review_score` | 0 reviews | 1-9 reviews | ≥ 10 reviews |
|---|---|---|---|
| 0 | 35 | 23 | 0 |
| 3-8 | 0 | 0 | 42 |

For the 58 apps with score 0, `review_score_desc` reads `'No user reviews'` or `'N user reviews'`.

**Unusual values** (notebook §8): review counts.

| Method | Threshold | Flags |
|---|---|---|
| IQR fence, raw scale | 222 reviews | 11 of 65 apps |
| mean + 3 SD (not used) | 9,181 | 2 |
| robust z (median/MAD) on log10 > 3.5 | — | 0 |
| **IQR fence on log10 (used)** | **4,504 reviews** | **3: ELEX 19,627, Timberman 11,158, Life is Strange: Reunion 5,747** |

**Text in the catalogue** (notebook §5): 190,242 app names.

| Check | Names |
|---|---|
| not NFC | 2 |
| leading / trailing whitespace | 213 |
| double spaces | 270 |
| control character (a tab) | 1 |
| changed by NFKC (full-width forms) | 2,564 |
| contain Turkish letters (`İ ı Ş ş Ğ ğ`) | 27 |
| empty (NULL since M2) | 18 |

## 3. Issues register

| ID | Family | Table.column | What | Where / when / how many | Evidence | Cause | Decision | Flag | Investigated by |
|---|---|---|---|---|---|---|---|---|---|
| I-01 | 1 Missing | app_detail.release_date | No full release date | 27 games: `Coming soon` 11, `To be announced` 9, year 4, month 2, quarter 1 | all 27 `coming_soon = 1` (notebook §2) | Confirmed: the store says the game is not released and gives no day | date NULL; year kept from partial text (7 games); precision recorded | `missing_not_announced`, `partial_year`, `partial_month`, `partial_quarter` | Yasin Gündoğan |
| I-02 | 2 Invalid | app_detail.release_date | Dates after the retrieval day | 3 games (2026-10-27, 2026-10-30, 2026-11-03) | all 3 `coming_soon = 1` | Confirmed: announced release dates | kept | `announced_future` | Yasin Gündoğan |
| I-03 | 1 Missing | app_detail.price_* | No `price_overview` | 40 games | 12 free, 28 unreleased paid, 0 released paid | Inferred: free games have no price; unreleased games have none yet | NULL (not 0) | `missing_free`, `missing_unreleased` | Yasin Gündoğan |
| I-04 | 1 Missing | app_detail.metacritic_score | Almost always missing | 99 of 100 | only ELEX (67) has a score | Inferred: Metacritic reviews few small games | kept as is; not usable | `missing_not_reported` | Yasin Gündoğan |
| I-05 | 1 Missing | app_detail.recommendations_total | Mostly missing | 91 of 100 | present only for 9 paid apps with ≥ 149 reviews; absent for free games with 357 and 365 reviews | Unknown: depends on `is_free` and on something not in the data | kept as is; not usable | `missing_not_reported` | Muhammet Mustafa Sarıbuğa |
| I-06 | 2 Invalid | review_snapshot.review_score | 0 used as "no score" | 58 apps: 35 with 0 reviews, 23 with 1-9 | every app with < 10 reviews has 0 and a text instead of a label; every app with ≥ 10 has 3-8 | Inferred (strong): Steam shows a score only from 10 reviews | score and label NULL | `missing_no_reviews`, `missing_too_few_reviews` | Muhammet Mustafa Sarıbuğa |
| I-07 | 1 Missing | review_snapshot.total_reviews | Zero reviews | 35 apps: 30 unreleased, 5 released | all 30 unreleased games have 0 reviews; no unreleased game has any | Confirmed: nobody can review an unreleased game; the 5 released are real zeros | kept as 0 | `zero_reviews` | Muhammet Mustafa Sarıbuğa |
| I-08 | 5 Text | app.name | Invisible text differences | 477 catalogue names | 2 non-NFC, 213 edge spaces, 270 double spaces, 1 tab | Unknown: as sent by the app list | NFC, control char → space, whitespace collapsed | `text_normalised` | Emre Kahraman |
| I-09 | 1 Missing | app.name | Empty catalogue name | 18 apps | NULL since M2 (`''` in the raw list) | Unknown | NULL | `missing_not_provided` | Emre Kahraman |
| I-10 | 6 Identity | category.description | Two ids, one label | ids 55/56 (DualShock), 57/58 (DualSense) | every app with 56 also has 55 (5 of 5); same for 58/57 | Unknown: Steam does not document the difference | kept as separate categories; 56 and 58 relabelled `(id 56)` / `(id 58)` | `duplicate_label` | Emre Kahraman |
| I-11 | 5 Text | category.description | Label is a localisation key | id 80, 9 apps | `'#category_playable_at_your_own_pace'`; all 9 apps also have id 74 | Inferred: Steam has no English text for this id | label read from the key: `Playable at your own pace` | `placeholder_label` | Emre Kahraman |
| I-12 | 5 Categories | genre.description | Two "genres" are not genres | id 37 Free To Play (10 apps), id 70 Early Access (5) | all 10 F2P apps `is_free = 1`; all 5 EA apps released 2018-2025 | Inferred: a price model and a release status | kept; `kind` = `business_model` / `release_status` | `kind_flag = mapped` | Yasin Gündoğan |
| I-13 | 6 Identity | publisher.name | Publisher spelled differently from the developer | `Relaxygen studios`, `Jesse Carradine?` | same app credits `Relaxygen Studios` / `Jesse Carradine` as developer; equal name keys; `?` already in raw JSON | Inferred: one company each, typed twice | mapped to the developer spelling | `alias_mapped` | Emre Kahraman |
| I-14 | 6 Identity | app.name | Sampled game shares a name with another app | 7 games, 9 other appids | equal name keys (`Presence` ×3, `Dice`, `Monster Collector`, …) | Unknown: the other apps were not sampled, so nothing to compare | kept separate; decisions in `app_identity.csv` | — (mapping table) | Muhammet Mustafa Sarıbuğa |
| I-15 | 1 Missing | app_detail (coverage) | Unequal coverage by year; one run | 1-16 games per year; 2013, 2015, 2023: 1 each | notebook §1 | Confirmed: `MAX_APPS = 100` random sample, one ingest run | no cleaning; M4 must not compare small years | — | Yasin Gündoğan |
| I-16 | 3 Unusual | review_snapshot.total_reviews | Very many reviews | 3 apps | above the log10 IQR fence (4,504) | Confirmed: well-known games (ELEX, Timberman, Life is Strange: Reunion) | kept | `unusual_high` | Muhammet Mustafa Sarıbuğa |
| I-17 | 4 Across fields | app_detail.recommendations_total vs review_snapshot.total_reviews | Two review counts that disagree | 9 apps | recommendations are 63-94 % of total reviews | Inferred: we request `purchase_type=all` (key activations included), while Steam counts only Steam purchases toward its score ([Steamworks: user reviews](https://partner.steamgames.com/doc/store/reviews)) | both kept; M4 uses `total_reviews` | — | Muhammet Mustafa Sarıbuğa |

### Checked and not found

| Family | Check | Result |
|---|---|---|
| 2 Invalid | negative price, discount outside 0-100, negative review count | 0 (also rejected by M2's CHECK constraints) |
| 2 Invalid | price in a currency other than USD | 0 of 60 |
| 2 Invalid | released game (`coming_soon = 0`) dated after the retrieval day | 0 |
| 2 Invalid | release year before 1970 or more than 5 years ahead | 0 (range 2013-2027) |
| 2 Invalid | `is_free = 1` with a price | 0 |
| 3 Unusual | price above the log10 IQR fence (112.37 USD) | 0 (max 39.99 USD) |
| 4 Across fields | `total_positive + total_negative <> total_reviews` | 0 of 100 |
| 4 Across fields | final price > initial price; final ≠ initial × (1 − discount) by more than 1 cent | 0; largest gap 0.85 cent (Steam rounds down) |
| 4 Across fields | score label outside its share band (≥ 10 reviews) | 0 of 42; closest: 79.7 % for "Mostly Positive" (band 70-79) |
| 4 Across fields | unreleased game with reviews; coming soon but dated in the past | 0; 0 |
| 4 Sequence | high appid with an old release date (re-registered game) | 0: the earliest date per 500k-appid band never goes back |
| 5 Text | non-NFC, edge or double spaces in the 100 store names, 107 developer, 101 publisher names | 0 |
| 6 Identity | two developer (or two publisher) names that differ only in case | 0 |
| 6 Identity | duplicate keys in M2 | 0 (M2 natural keys are UNIQUE; checked again by `checks.py`) |

## 4. Missing values

**Release date (I-01).** 27 games, all with `coming_soon = 1`. The date is not lost: the publisher has not
announced a day. The mechanism is **MAR given `coming_soon`**: whether the date is missing is fully explained
by an observed field. For seven of them the text still gives a year (`2026`, `Q4 2026`, `October 2026`), which
the cleaned layer keeps in `release_year` with `release_precision`, never as an invented day. *For M4:* counts
of "games released per year" must use `coming_soon = 0`; 2026 is incomplete by construction, and the 20 not
announced games belong to no year.

**Price (I-03).** 40 games without a price: 12 free, 28 unreleased paid. MAR given `is_free` and `coming_soon`.
NULL is kept rather than writing 0 for free games: "free" is already a column, and an analysis that wants 0
writes `COALESCE(price_initial_cents, 0) … WHERE is_free = 1` where it can be seen. *For M4:* price
comparisons are over the 60 released paid games only.

**Review score (I-06).** The biggest pattern: 58 of 100 apps. The score is missing by a deterministic rule on an
observed count (fewer than 10 reviews), so given `total_reviews` it is **MAR**. But for the question "how
satisfied are players with games from year X?" the missingness is related to what we want to measure: games
nobody reviews are less popular, and popularity and satisfaction are plausibly related. Relative to the project
question we therefore treat it as **MNAR**: we cannot know the positive share of games too few people
reviewed, and it is probably not the same as for reviewed games. It is concentrated in recent years (released
games without a usable score: 2021 4 of 6, 2022 6 of 9, 2025 4 of 7, 2026 8 of 13) and among free and cheap
games. This is the Bodrum lesson from the lab: an average over the games that have a score looks clean
because of the games that do not. *For M4:* report positive share by year only for games with a usable score,
say how many games that is per year, and never fill the share of a game with fewer than 10 reviews.

**Zero reviews (I-07).** 35 apps: 30 unreleased (structural, MAR given `coming_soon`), 5 released and truly
unreviewed. These zeros are measurements, kept as 0 with `zero_reviews`, so a share computed from them is
undefined, not 0.

**Recommendations and Metacritic (I-04, I-05).** `metacritic_score` exists for one game: critics cover mostly
larger games, so missingness depends on unobserved critic coverage (MNAR). `recommendations_total` is missing for
every free game regardless of how many reviews it has, and for paid games with up to 893 reviews; the rule is
not visible in the data (**unknown**). *For M4:* neither column is used.

## 5. Flags and rules

Each flag column applies its rules in order; the first rule that applies wins. Where the source delivers values
as one object, one flag covers the group: the price (currency, initial, final, discount) arrive together in
`price_overview` and are missing or present together, and the three review counts must be judged together
because one rule (positive + negative = total) involves all three. One flag per group keeps the count of
"usable prices" a single filter instead of four.

**`app_detail_clean.release_date_flag`** — `_raw` is the source text (`app_detail.release_date_text`).

| Order | Flag | Meaning | Rule (with constants) | Cleaned value | Count |
|---|---|---|---|---|---|
| 1 | `missing_not_reported` | no date and no text | text and parsed date both NULL | NULL | 0 |
| 2 | `missing_not_announced` | store says there is no date | text in `NOT_ANNOUNCED_TEXTS` | NULL, year NULL | 20 |
| 3a | `invalid_implausible` | year out of range | year < `MIN_PLAUSIBLE_RELEASE_YEAR` (1970) or > retrieval year + `MAX_ANNOUNCE_YEARS_AHEAD` (5) | NULL | 0 |
| 3b | `invalid_future` | released game dated after retrieval | date > retrieval day and `coming_soon ≠ 1` | NULL | 0 |
| 3c | `announced_future` | announced day | date > retrieval day and `coming_soon = 1` | date kept | 3 |
| 3d | `ok` | full date on or before retrieval | — | unchanged | 70 |
| 4 | `partial_quarter` | `Q4 2026` | `^Q[1-4] YYYY$` | date NULL, year kept | 1 |
| 5 | `partial_month` | `October 2026` | `Month YYYY` with a real month | date NULL, year kept | 2 |
| 6 | `partial_year` | `2026` | `^YYYY$` | date NULL, year kept | 4 |
| 7 | `invalid_unparseable` | anything else | — | NULL | 0 |

**`app_detail_clean.price_flag`**

| Order | Flag | Meaning | Rule | Cleaned value | Count |
|---|---|---|---|---|---|
| 1 | `missing_free` | free game, no price object | price NULL and `is_free = 1` | NULL | 12 |
| 2 | `missing_unreleased` | not released, no price yet | price NULL and `coming_soon = 1` | NULL | 28 |
| 3 | `missing_not_reported` | released, paid, no price | price NULL otherwise | NULL | 0 |
| 4 | `invalid_currency` | not the currency we asked for | currency ≠ `EXPECTED_CURRENCY` (USD) | NULL (unit unknown) | 0 |
| 5 | `invalid_inconsistent` | parts do not add up | final > initial, or \|final − initial × (100 − discount)/100\| > `PRICE_ROUNDING_TOLERANCE_CENTS` (1) | NULL | 0 |
| 6 | `unusual_high` | extreme but possible | log10(initial) > Q3 + `IQR_K` × IQR over usable prices (fence 112.37 USD) | kept | 0 |
| 7 | `ok` | — | — | unchanged | 60 |

**`review_snapshot_clean.review_counts_flag`** and **`review_score_flag`**

| Order | Flag | Meaning | Rule | Cleaned value | Count |
|---|---|---|---|---|---|
| 1 | `missing_not_reported` | a count is missing | any count NULL | NULL | 0 |
| 2 | `invalid_sum_mismatch` | counts do not add up | positive + negative ≠ total | NULL (all three) | 0 |
| 3 | `zero_reviews` | a real zero | total = 0 | 0 kept | 35 |
| 4 | `unusual_high` | extreme but possible | log10(total) > Q3 + `IQR_K` × IQR over apps with reviews (fence 4,504) | kept | 3 |
| 5 | `ok` | — | — | unchanged | 62 |
| s1 | `missing_not_reported` | no score or count | score or total NULL | NULL | 0 |
| s2 | `missing_no_reviews` | sentinel 0, no reviews | total = 0 | score and label NULL | 35 |
| s3 | `missing_too_few_reviews` | sentinel 0, 1-9 reviews | total < `MIN_REVIEWS_FOR_SCORE` (10) | score and label NULL | 23 |
| s4 | `invalid_sentinel` | 0 although enough reviews | score = 0 | NULL | 0 |
| s5 | `invalid_inconsistent` | label does not match counts | share outside `SCORE_SHARE_BANDS[score]` ± `SCORE_BAND_TOLERANCE_PCT` (3) | NULL | 0 |
| s6 | `ok` | — | — | unchanged | 42 |

**Other flag columns**

| Table.flag | Order: flag — rule — cleaned value | Counts |
|---|---|---|
| `app_clean.name_flag` | 1 `missing_not_provided` — raw NULL — NULL · 2 `text_normalised` — NFC / control → space / whitespace collapsed changes the text — normalised · 3 `ok` | 18 · 477 · 189,747 |
| `app_detail_clean.store_name_flag` | same rules as `app_clean.name_flag` | 0 · 0 · 100 |
| `app_detail_clean.metacritic_score_flag` / `recommendations_total_flag` | 1 `missing_not_reported` — raw NULL · 2 `ok` — unchanged | 99 · 1 / 91 · 9 |
| `developer_clean.name_flag` / `publisher_clean.name_flag` | 1 `alias_mapped` — a `same` decision in `company_match.csv` — canonical name · 2 `text_normalised` · 3 `ok` | developer 0 · 0 · 107; publisher 2 · 0 · 99 |
| `genre_clean.description_flag` / `kind_flag` | text rules as above / `mapped` if `genre_kind.csv` has the id, else `unmapped` with `kind` NULL | 12 ok / 12 mapped |
| `category_clean.description_flag` | 1 `placeholder_label` / `duplicate_label` — a row in `category_label.csv` whose `description_raw` matches — the decided label · 2 `missing_not_reported` · 3 `text_normalised` · 4 `ok` | 1 · 2 · 0 · 0 · 46 |

### Constants

All in `src/clean_config.py`.

- `NOT_ANNOUNCED_TEXTS = ("coming soon", "to be announced", "tba", "tbd")`: the two texts in the profile plus their
  usual abbreviations; an unlisted text falls to `invalid_unparseable`, so a new wording is counted, not hidden.
- `MIN_PLAUSIBLE_RELEASE_YEAR = 1970`: Steam sells re-releases of very old games, so this only catches parse errors
  (years like 0019); the sample's earliest year is 2013, so moving it anywhere below 2013 changes nothing.
- `MAX_ANNOUNCE_YEARS_AHEAD = 5`: the furthest announcement is one year ahead (`2027`); at 0 the 2027 game would
  become `invalid_implausible`.
- `EXPECTED_CURRENCY = "USD"`: `ingest.py` requests the store with `cc=us`; any other currency means the request
  changed, and a price in an unknown unit is not comparable.
- `PRICE_ROUNDING_TOLERANCE_CENTS = 1`: Steam rounds discounted prices down (599 × 25 % = 149.75 → 149); the largest
  gap is 0.85 cent; at 0, 39 of the 60 prices would wrongly become `invalid_inconsistent`.
- `MIN_REVIEWS_FOR_SCORE = 10`: the profile's split is exact (all 58 apps below 10 have score 0, all 42 at or above
  have 3-8); at 50, 20 real scores would become `missing_too_few_reviews` (ok 42 → 22) — tested and reverted.
- `SCORE_SHARE_BANDS`: the share ranges behind Steam's labels, from a third-party summary
  ([bugnet](https://bugnet.io/blog/steam-review-score-thresholds-explained), which calls them approximate) and
  checked on our data: all 42 scored apps fall inside their band.
- `SCORE_BAND_TOLERANCE_PCT = 3`: our counts include key activations while Steam's score does not (I-17), so a share
  close to a band edge can drift; the closest app is 0.33 points inside its band, and at 0, 1, 2 or 3 the count of
  inconsistent scores stays 0 — the value is slack for future snapshots, a guess we will revisit with more data.
- `IQR_K = 1.5`: the standard fence; on log10 it flags 3 apps; at 1.0 still 3, at 2.0 only ELEX, at 3.0 none. For
  prices it flags nothing at 1.0 or 1.5 and three normal full-game prices at 0.5.
- `MIN_VALUES_FOR_FENCE = 20`: with fewer values Q1 and Q3 are two of the data points; prices (60) and review counts
  (65) are well above it; with 1-16 games per year this is also why we use one fence across apps, not one per year.
- `FUZZY_CUTOFF = 0.80`: at 0.85 the fuzzy step proposes nothing; at 0.80 it proposes 3 borderline pairs, which a
  person rejected; at 0.75 it proposes 8, all sharing only *Games* or *Entertainment*. We keep 0.80 so borderline
  pairs reach a person; nothing is merged without a row in `company_match.csv`.
- `MAX_MISSING_SCORE_SHARE = 0.50`, `MIN_GAMES_FOR_YEAR_CHECK = 5`: above half, a year's positive share describes a
  minority of its games; years with fewer than 5 released games are too small to judge at all. At 0.6, 2025 drops
  out of the WARN; at 0.7 no year warns.
- `MAX_SNAPSHOT_AGE_DAYS = 7`: M5 will schedule ingestion; a week is the loosest cadence that still yields two
  snapshots inside a two-week milestone. The single snapshot is 24 days old today, so this WARNs until M5.

## 6. Mapping and identity decisions

Matching runs in four steps: **exact → normalised key → fuzzy proposal → human decision**. The key
(`clean.name_key`) is NFKC (folds full-width letters), then explicit Turkish folding (`İ→i`, `ı→i`, `ş→s`, …,
because `'İ'.lower()` is `'i'` plus a combining dot), then `casefold()`, then letters and digits only. A name
with no letter or digit (`'-'`, `'***'`, `'∞'`: 8 catalogue apps) gets no key and never matches. The key only
proposes; cleaned names stay NFC with their original case and punctuation.

**Developers and publishers** ([`company_match.csv`](../data/mappings/company_match.csv)).

| Step | Pairs | Result |
|---|---|---|
| Exact | 79 of 101 publisher names are also developer names | self-published; no decision needed |
| Normalised key | 2 | `Relaxygen studios` → `Relaxygen Studios`; `Jesse Carradine?` → `Jesse Carradine` (`same`, inferred: same app) |
| Fuzzy ≥ 0.80 | 3 | `KPP` / `BFG` / `Novark Entertainment`: only *Entertainment* shared, different apps (`separate`) |
| Human decisions | 5 of 5 proposals decided | 2 aliases applied, 0 undecided, 0 decisions that match nothing |

An earlier version of the key kept only ASCII letters and gave the studios `本格工作室` and `虚一工作室` the
same empty key — the notebook (§6) shows it; the final key keeps every Unicode letter.

**Genres and categories** ([`genre_kind.csv`](../data/mappings/genre_kind.csv),
[`category_label.csv`](../data/mappings/category_label.csv)). All 12 genre ids have a decided `kind`: 10 `genre`,
`Free To Play` = `business_model`, `Early Access` = `release_status` (evidence in I-12). Three category ids are
relabelled (I-10, I-11); the decision applies only while Steam still sends the `description_raw` recorded in
the CSV, so a label Steam fixes later shows up as an unmatched decision instead of being overwritten.

**Apps** ([`app_identity.csv`](../data/mappings/app_identity.csv)). For each sampled game, the key of its
catalogue name and of its store name is compared with all 190,242 catalogue keys: 9 proposals for 7 games, all
decided `separate` (8 with basis `unknown`, 1 `inferred`: `Di[c]E` has the lower appid, so it existed before
`Dice` and cannot be its re-registration). No pair is merged, so `app_resolved` maps every app to itself;
because the other apps have no store page or snapshot, keeping them apart changes no per-app statistic. The
catalogue as a whole has 3,242 keys shared by more than one appid; these are reported by `clean.py` but not
decided, because no analysis uses unsampled apps.

All four CSVs are loaded into STRICT tables on every run; genre, category and app decisions have foreign keys
to the Steam ids in the M2 entity tables, so a decision about an id that does not exist stops the build.

## 7. Counts and validation

`python src/clean.py` (run twice; the second run prints the same and leaves every table byte-identical):

```
clean.py - cleaned layer in data/steam_catalog.db
app_clean:             190,242 rows (raw app: 190,242)
  name_flag                    ok 189,747 · missing_not_provided 18 · text_normalised 477
app_detail_clean:      100 rows (raw app_detail: 100)
  store_name_flag              ok 100 · missing_not_provided 0 · text_normalised 0
  release_date_flag            ok 70 · announced_future 3 · missing_not_reported 0 · missing_not_announced 20 · partial_year 4 · partial_quarter 1 · partial_month 2 · invalid_future 0 · invalid_implausible 0 · invalid_unparseable 0
  price_flag                   ok 60 · missing_free 12 · missing_unreleased 28 · missing_not_reported 0 · invalid_currency 0 · invalid_inconsistent 0 · unusual_high 0
  metacritic_score_flag        ok 1 · missing_not_reported 99
  recommendations_total_flag   ok 9 · missing_not_reported 91
review_snapshot_clean: 100 rows (raw review_snapshot: 100)
  review_counts_flag           ok 62 · zero_reviews 35 · missing_not_reported 0 · invalid_sum_mismatch 0 · unusual_high 3
  review_score_flag            ok 42 · missing_no_reviews 35 · missing_too_few_reviews 23 · missing_not_reported 0 · invalid_sentinel 0 · invalid_inconsistent 0
developer_clean:       107 rows (raw developer: 107)
  name_flag                    ok 107 · text_normalised 0 · alias_mapped 0
publisher_clean:       101 rows (raw publisher: 101)
  name_flag                    ok 99 · text_normalised 0 · alias_mapped 2
genre_clean:           12 rows (raw genre: 12)
  description_flag             ok 12 · missing_not_reported 0 · text_normalised 0
  kind_flag                    mapped 12 · unmapped 0
category_clean:        49 rows (raw category: 49)
  description_flag             ok 46 · missing_not_reported 0 · text_normalised 0 · placeholder_label 1 · duplicate_label 2

review_score_flag by release year (app_detail_clean.release_year):
  year           missing_no_reviews  missing_too_few_reviews                       ok
  2013                            0                        0                        1
  2015                            0                        0                        1
  2016                            0                        0                        3
  2017                            0                        1                        3
  2018                            0                        1                        6
  2019                            0                        1                        2
  2020                            0                        2                        3
  2021                            0                        4                        2
  2022                            1                        5                        3
  2023                            0                        0                        1
  2024                            0                        1                        9
  2025                            1                        3                        3
  2026                           12                        5                        5
  2027                            1                        0                        0
  none                           20                        0                        0

statistics:
  price fence: log10(price_initial_cents) <= 4.051 (= 112.37 USD) over 60 usable prices
  review fence: log10(total_reviews) <= 3.654 (= 4,504 reviews) over 65 snapshots with reviews
mappings:
  rows loaded: company_match 5 · genre_kind 12 · category_label 3 · app_identity 9
  company names: exact 79 publisher names = developer names · normalised key 2 proposal(s) · fuzzy (>= 0.8) 3 proposal(s) · decided 5 of 5 · aliases applied 2
  genres: kind mapped 12 of 12 (unmapped: none)
  category labels: applied 3 of 3 decision(s)
  app identity: 9 name-key proposal(s) for sampled apps · decided 9 · same 0 · separate 9
  catalogue name-key groups with more than one app: 3,242 (reported, not merged: only sampled apps are checked)
  decisions that matched nothing: 0
  proposals without a decision: 0
M2 tables unchanged:   yes (sha256 of all 12 M2 tables before = after)
foreign_key_check:     OK
STATUS: COMPLETE
```

`python src/validate.py --as-of 2026-10-27` (exit 0):

```
PASS  row count app_clean = app                          190,242 = 190,242
PASS  row count app_detail_clean = app_detail            100 = 100
PASS  row count review_snapshot_clean = review_snapshot  100 = 100
PASS  row count developer_clean = developer              107 = 107
PASS  row count publisher_clean = publisher              101 = 101
PASS  row count genre_clean = genre                      12 = 12
PASS  row count category_clean = category                49 = 49
PASS  no impossible cleaned price                        0 row(s): negative, final > initial, discount outside 0-100, not USD, or discount off by > 1 cent
PASS  no impossible cleaned review counts                0 row(s): negative, or positive + negative <> total
PASS  no review-score sentinel survives                  0 score(s) outside 1-9 or with fewer than 10 reviews
PASS  no impossible cleaned release date                 0 row(s): invalid date, future while released, or year outside 1970..retrieval + 5
PASS  foreign_key_check                                  OK
PASS  flags agree with cleaned values                    10 value groups checked, 0 disagree
WARN  usable review score per release year               7 years with >= 5 released games; over 50% without a usable score: 2021 67% (4/6), 2022 67% (6/9), 2025 57% (4/7), 2026 62% (8/13)
PASS  every sampled app in the latest snapshot           100 of 100 (resolved) on 2026-10-03
WARN  latest snapshot <= 7 days old                      2026-10-03 is 24 day(s) old on 2026-10-27
WARN  no unusual_high review counts                      3: ELEX 19,627, Timberman 11,158, Life is Strange: Reunion 5,747
PASS  recommendations_total <= total_reviews             0 of 9 apps with both have more recommendations than reviews
PASS  no placeholder, shared or unmapped labels          0 problem(s) in category_clean / genre_clean
PASS  every proposed company match has a decision        5 proposal(s), 0 without a row in data/mappings/company_match.csv

20 checks: 17 PASS · 3 WARN · 0 STOP
STATUS: OK
```

**Each WARN.**

| WARN | Problem in … | Why |
|---|---|---|
| usable review score per release year | **the world** | recent and small games have not collected 10 reviews yet; nothing is wrong with the data, but year comparisons of positive share stand on few games (section 4) |
| latest snapshot ≤ 7 days old | **the pipeline** | there is one manual ingest run (2026-10-03); M5 scheduling will clear it |
| no unusual_high review counts | **the world** | three genuinely popular games; flagged and kept so M4 can show results with and without them |

**Each STOP check, made to fail once** (`python src/validate.py --prove-stops`: each line breaks a temporary copy
of the database, runs `validate.py` on it, and confirms STOP and a non-zero exit):

```
OK   row count app_detail_clean = app_detail: delete one row of app_detail_clean -> STOP, exit 1
OK   no impossible cleaned price: make one kept final price 1 USD above its initial price (raw and cleaned) -> STOP, exit 1
OK   no impossible cleaned review counts: add 1 to one app's positive count (raw and cleaned) -> STOP, exit 1
OK   no review-score sentinel survives: keep the raw score 0 of one app with 1-9 reviews and call it ok -> STOP, exit 1
OK   no impossible cleaned release date: give one released game the date 2099-01-01 -> STOP, exit 1
OK   foreign_key_check: point one cleaned review row at an app_key that does not exist (foreign keys off) -> STOP, exit 1
OK   flags agree with cleaned values: set one cleaned price to NULL while its flag still says ok -> STOP, exit 1

7/7 STOP checks failed when they should
```

**How the cleaning code was checked.** Raw untouched: `clean.py` fingerprints the 12 M2 tables (sha256 of every
row) before and after each run and fails if they differ; every write statement in it is checked at import to
target a cleaned or mapping table. Idempotent: two runs give identical fingerprints for all 23 tables; changing
`MIN_REVIEWS_FOR_SCORE` to 50 changed only `review_snapshot_clean`, and changing it back restored every table
exactly. Atomic: a trigger that aborts the insert into `review_snapshot_clean` (the last table written) makes the
run print `STATUS: FAILED - rolled back` and exit 1, with every table identical to before. M2 still holds:
`python src/load.py` after `clean.py` changes nothing, and `python src/checks.py` passes 6/6.

## 8. What we can't say

- **Years with fewer than five games** (2013, 2015, 2016, 2017, 2019, 2023) cannot be compared with anything: one
  game is not a year. Even the largest year has 16 games. The sample is 100 of 190,242 apps; M4's results
  describe this sample, with wide uncertainty, until ingestion covers more apps.
- **Positive review share** is only known for 42 games. For the other 58 it is not low or high but unknown, and
  probably different from the reviewed games (section 4). Every positive-share result must say how many games it
  stands on, and the 2021, 2022, 2025 and 2026 cohorts are mostly unreviewed.
- **Change over time** — of reviews, prices or discounts — cannot be seen from one snapshot. Discounts in
  particular are a one-day picture (39 of 60 paid games were discounted on 2026-10-03), so "price" in M4 should
  be `price_initial_cents`, not the final price.
- **Two review counts.** `total_reviews` includes key activations; Steam's own score and `recommendations_total`
  do not (I-17). We use `total_reviews` throughout but cannot reproduce Steam's exact score from it.
- **Identity of same-named apps** (I-14) and the meaning of categories 56 and 58 (I-10) are unknown; we would
  revisit both if the unsampled apps' store pages are ever fetched.
- **Genre shares.** M4 should count genres with `kind = 'genre'` from `app_genre_clean_v`; `Indie` is kept as a
  genre because the M0 question uses it, although it describes the developer rather than the gameplay.
- **Metacritic and recommendations** (I-04, I-05) are effectively absent and should not be used.
