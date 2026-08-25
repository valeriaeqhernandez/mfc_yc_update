"""
Query templates for a16z Speedrun pre-Demo-Day sourcing.

Mirrors the role of the query-building logic in li_search_selenium.py /
li_native_search.py, but tuned for Speedrun's bio/announcement conventions
instead of YC's "(YC F26)" convention.

CURRENT_BATCH comes from sr_config.json's "batch" field; change it with
`python sr_configure.py` (option to change batch) rather than editing this
file, so every downstream file (roster, classified roster, last-sent
tracking, snapshots) picks up the new batch consistently. See README.md,
"Switching to a new batch."
"""

import re

from sr_config import load_config

CURRENT_BATCH = load_config()["batch"]
_match = re.match(r"SR(\d+)", CURRENT_BATCH)
CURRENT_BATCH_NUM = _match.group(1) if _match else CURRENT_BATCH

# High-precision bio/title patterns. These are the strings people actually
# put in LinkedIn headlines and post signatures; confirmed via manual
# Google/LinkedIn spot checks before wiring up automation (same validation
# discipline as the YC pipeline).
BATCH_BIO_PATTERNS = [
    f"({CURRENT_BATCH})",
    f"(a16z {CURRENT_BATCH})",
    f"(a16z speedrun {CURRENT_BATCH})",
]

# Search queries for Google (via sr_google_search.py) and LinkedIn native
# search (via sr_li_native_search.py). Kept short/composable: long combined
# queries return worse results, same lesson learned on the YC side.
SEARCH_QUERIES = [
    f'"({CURRENT_BATCH})"',
    f'"a16z speedrun {CURRENT_BATCH}"',
    f'"a16z speedrun {CURRENT_BATCH_NUM}"',
    f'"speedrun{CURRENT_BATCH_NUM}"',
    f'"joined a16z speedrun {CURRENT_BATCH}"',
    f'"joined a16z speedrun" "{CURRENT_BATCH}"',
    f'"backed by a16z speedrun" "{CURRENT_BATCH}"',
    f'"selected for a16z speedrun" "{CURRENT_BATCH}"',
    f'"part of a16z speedrun {CURRENT_BATCH}"',
    # a16z staff (Jordan Carver, Macy Mills, Andrew Chen, Tom Hammer, etc.)
    # post portfolio company names in hiring/office-hours/event posts, often
    # *before* founders themselves post; worth querying by role instead of
    # just by founder-side language.
    f'"a16z speedrun" hiring "{CURRENT_BATCH}"',
    f'"a16z speedrun" "{CURRENT_BATCH}" raised',
]

# LinkedIn People-search specific: search headline field for the bio tag.
LINKEDIN_PEOPLE_SEARCH_QUERIES = [
    f"({CURRENT_BATCH})",
    f"a16z speedrun {CURRENT_BATCH}",
]

# Noise filters: matches that should NOT be treated as a join signal even
# though they contain batch keywords. Applications-open/close announcements
# and scout/referral posts are the dominant noise source (same category of
# noise as YC's own recruiting posts).
NOISE_PATTERNS = [
    re.compile(r"applications? (are|is) (now )?open", re.I),
    re.compile(r"applications? (have )?closed", re.I),
    re.compile(r"apply (now|before|by)", re.I),
    re.compile(r"deadline to apply", re.I),
    re.compile(r"\bscout\b.*\brefer\b", re.I),
    re.compile(r"DM me (if|what) you", re.I),
    re.compile(r"not selected", re.I),  # e.g. "SpeedRun 2025: GIBBI (Not Selected)"
]

# Prior-batch exclusion: a company tagging itself with an OLDER batch code
# should never be attributed to CURRENT_BATCH just because it also mentions
# "a16z speedrun" generically. Extraction must anchor on the specific code.
PRIOR_BATCH_CODES = [f"SR{n:03d}" for n in range(1, int(CURRENT_BATCH_NUM))]
