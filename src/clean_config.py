"""Every threshold and constant used by src/clean.py and src/validate.py.

One place, each with a name and a comment. Every constant is justified in
docs/cleaning_report.md (section 5, "Constants"). Change a value here, run
python src/clean.py again, and exactly what the value affects changes;
change it back and the cleaned layer is restored.
"""

# ------------------------------------------------------------ release dates
# Text Steam writes instead of a date when none is announced (compared after
# NFC + strip + casefold). Seen in the profile: 'Coming soon' (11), 'To be announced' (9).
NOT_ANNOUNCED_TEXTS = ("coming soon", "to be announced", "tba", "tbd")

# Earliest plausible release year. Steam sells re-releases of 1970s-80s games,
# so this only catches parse errors (year 0019, 1900 ...), not old games. Report section 5.
MIN_PLAUSIBLE_RELEASE_YEAR = 1970

# Latest plausible year of an *announced* date, relative to the retrieval year.
# The furthest announcement in the sample is 1 year ahead ('2027'). Report section 5.
MAX_ANNOUNCE_YEARS_AHEAD = 5

# ------------------------------------------------------------------- prices
# The store was requested with cc=us (src/ingest.py), so every price must be USD.
EXPECTED_CURRENCY = "USD"

# Steam rounds discounted prices to whole cents; allow 1 cent between
# final and initial * (100 - discount) / 100. Largest gap in the sample: 0.85 cent. Report section 5.
PRICE_ROUNDING_TOLERANCE_CENTS = 1

# ------------------------------------------------------------------ reviews
# Steam shows a review score only from this many reviews on; below it the
# source writes review_score = 0 and review_score_desc = 'N user reviews'.
# Profile: 58 of 58 apps with 1-9 or 0 reviews have score 0; 42 of 42 apps with >= 10 have 3-8.
MIN_REVIEWS_FOR_SCORE = 10

# Share of positive reviews (percent) each Steam review score stands for:
# score -> [lowest share, highest share), half-open. 1-3 = negative labels,
# 4 Mostly Negative, 5 Mixed, 6 Mostly Positive, 7-9 positive labels. Report section 5.
SCORE_SHARE_BANDS = {
    1: (0, 20), 2: (0, 20), 3: (0, 20),
    4: (20, 40), 5: (40, 70), 6: (70, 80),
    7: (80, 101), 8: (80, 101), 9: (95, 101),
}

# Steam computes the score on Steam purchases only, while our counts include
# key activations (purchase_type=all), so a share near a band edge may fall a
# little outside its band. Points of slack before we call a score inconsistent. Report section 5.
SCORE_BAND_TOLERANCE_PCT = 3

# ------------------------------------------------------------ unusual values
# Outlier rule for counts and prices that span orders of magnitude: IQR fence
# on log10(value). Upper fence = Q3 + IQR_K * IQR. Flagged values are kept. Report section 5.
IQR_K = 1.5

# Do not compute a fence on fewer values than this: with a handful of values,
# Q1 and Q3 are just two of the data points. Report section 5.
MIN_VALUES_FOR_FENCE = 20

# ---------------------------------------------------------- names / matching
# difflib ratio on name keys from which two different developer / publisher
# names are proposed to a person as possibly the same company. Report section 6.
FUZZY_CUTOFF = 0.80

# ---------------------------------------------------------------- validation
# WARN when, in a release year with at least MIN_GAMES_FOR_YEAR_CHECK games,
# more than this share of games has no usable review score. Report section 7.
MAX_MISSING_SCORE_SHARE = 0.50
MIN_GAMES_FOR_YEAR_CHECK = 5

# WARN when the newest review snapshot is older than this many days. Report section 7.
MAX_SNAPSHOT_AGE_DAYS = 7
