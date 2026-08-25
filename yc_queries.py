"""
yc_queries.py

Shared query patterns for the YC pre-MFN pipeline, based on how founders
actually write about being accepted into a YC batch. Mirrors sr_queries.py
for the Speedrun pipeline; unlike Speedrun, YC's batch tag isn't a saved
config value yet (li_unified_leads.py / li_search_selenium.py both take
it as a --batch-tag CLI argument per run), so this only holds phrasing
patterns, not a CURRENT_BATCH constant.

Previously each search surface only tried 1-2 patterns each (Google: 2,
LinkedIn Posts: 1, LinkedIn People: 1, just the bare tag) — confirmed live
that this misses real candidates who use phrasing like "backed by Y
Combinator" or "Y Combinator ... raised" instead of the narrower patterns
that were being searched for (the same class of gap found and fixed on
the Speedrun pipeline first). BASE_PATTERNS is deliberately shared across
all three search surfaces now.

BASE_PATTERNS are bare phrases (no "site:" operator) meant for LinkedIn's
own native search (Posts and People); li_search_selenium.py prepends
"site:linkedin.com " to each for Google search, since Google needs that
operator to restrict results to LinkedIn in the first place. Google and
LinkedIn Posts search use the full list; LinkedIn People search uses the
shorter LINKEDIN_PEOPLE_PATTERNS below instead (see the comment there).
"""

BASE_PATTERNS = [
    ("bio_tag", '"Co-founder" "{batch_tag}"'),
    ("announcement", '"{batch_tag}" "Y Combinator"'),
    ("join", '"{batch_tag}" "join"'),
    ("joined", '"joined Y Combinator" "{batch_tag}"'),
    ("backed", '"backed by Y Combinator" "{batch_tag}"'),
    ("selected", '"selected for Y Combinator" "{batch_tag}"'),
    ("hiring", '"Y Combinator" hiring "{batch_tag}"'),
    ("raised", '"Y Combinator" "{batch_tag}" raised'),
]

# LinkedIn People search specifically gets a shorter list than Google/Posts.
# Confirmed live, twice, with clean data: patterns requiring "Y Combinator"
# (spelled out) to co-occur with the "{batch_tag}" abbreviation are almost
# never how people actually write a short bio/headline — "join"/"joined"/
# "backed"/"selected"/"hiring"/"raised" all returned exactly 0 results
# across two separate real runs, while bio_tag and announcement returned
# 43 and 28. People search is also the expensive surface (up to 5 pages of
# pagination per query), so trimming the dead patterns here matters more
# than on Posts/Google, where the same patterns are cheap even when
# unproductive.
LINKEDIN_PEOPLE_PATTERNS = [
    ("bio_tag", '"Co-founder" "{batch_tag}"'),
    ("announcement", '"{batch_tag}" "Y Combinator"'),
]
