"""
sr_unified_leads.py: adapted from li_unified_leads.py

Biggest structural change from the YC pipeline: there is no yc_list_raw.py
equivalent to cross-reference against, because a16z publishes nothing about
an in-progress cohort. So this script can't "confirm a match against the
official directory"; it has to BE the directory, provisionally, until
Demo Day.

Consequences of that:
  1. Extraction has to be stricter up front (see EXTRACTION RULES below),
     since there's no later step to catch a bad match.
  2. Output is a running "provisional roster" file that accumulates across
     runs (sr_roster.json), not a per-run snapshot; a company seen once
     stays on the roster (with its first-seen date and evidence) until
     Demo Day either confirms or fails to confirm it.
  3. Corporate-suffix normalization still matters for dedup within this
     script's own output (e.g. "Munari" / "Munari Labs" / "Munari Labs Inc"
     should collapse to one roster entry).

EXTRACTION RULES (applied per raw hit before it's allowed onto the roster):
  - Must contain a CURRENT_BATCH-specific pattern (not just "a16z speedrun"
    generically, and not a PRIOR_BATCH_CODE; see sr_queries.py)
  - Must not match a NOISE_PATTERN (applications-open posts, scout/referral
    posts, "not selected" posts)
  - Must have an extractable company-name-shaped token near the batch
    pattern; heuristic: a capitalized noun phrase within ~40 chars of the
    match, optionally followed by a corporate suffix
"""

import json
import re
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from sr_queries import BATCH_BIO_PATTERNS, NOISE_PATTERNS, PRIOR_BATCH_CODES, CURRENT_BATCH

SNAP_DIR = Path("snapshots") / CURRENT_BATCH
# Namespaced by batch: otherwise switching CURRENT_BATCH (e.g. SR007 ->
# SR008) would silently merge the new batch's candidates into the old
# batch's roster, exactly the cross-batch contamination this pipeline is
# supposed to avoid (see README.md's "What carries over unchanged").
ROSTER_PATH = Path(f"sr_roster_{CURRENT_BATCH}.json")

# People-search hits where the batch tag matched but no company name could
# be resolved even after the Google follow-up (see resolve_ambiguous_via_
# google() below); kept separate from the roster since these are keyed by
# PERSON, not company, and are for manual review, not automated output.
UNRESOLVED_PATH = Path(f"sr_unresolved_people_{CURRENT_BATCH}.json")

SUFFIXES = [
    r"\binc\.?\b", r"\bllc\b", r"\bltd\.?\b", r"\bco\.?\b", r"\bcorp\.?\b",
    r"\bai\b", r"\blabs?\b", r"\btechnologies\b", r"\btech\b",
]
SUFFIX_RE = re.compile("|".join(SUFFIXES), re.I)

# Matches the batch tag itself ONLY; deliberately narrow (exact "SR007" or
# "speedrun007"/"speedrun 007") rather than a loose \d{3}/optional-zeros
# pattern. A real run showed the loose version matching itself: "SR007"
# would satisfy the whole old combined regex by having the name-capture
# group eat "SR00" and the tag alternation eat the trailing "7", producing
# a garbage "SR00" company out of thin air on every hit that merely
# contained the batch tag.
TAG_RE = re.compile(rf"SR{re.escape(CURRENT_BATCH[-3:])}|speedrun\s*{re.escape(CURRENT_BATCH[-3:])}", re.I)

# A candidate company-name-shaped token: 1-4 consecutive Capitalized words
# on the SAME line. Uses " +" rather than "\s+" between words deliberately:
# confirmed live that "\s+" crosses newlines and glues unrelated lines
# of scraped LinkedIn/Google text together (e.g. a post author's name on
# one line plus an engagement-count fragment on the next: "Kevin Jiang" +
# "Ca." became one bogus candidate "Kevin Jiang\nCa.").
NAME_RE = re.compile(r"[A-Z][A-Za-z0-9&.\-]*(?: +[A-Z][A-Za-z0-9&.\-]*){0,3}")

# Words/phrases that structurally look like a capitalized noun phrase but
# are never a real company name here; confirmed noise sources from a real
# run: role words, the program's own name, and German locale/engagement
# boilerplate that Google's snippet text concatenates right next to the
# actual post content ("vor 3 Wochen" = "3 weeks ago", i.e. "Wochen" reads
# as a capitalized German noun sitting right before the batch tag).
STOP_WORDS = {
    "the", "a", "our", "join", "joined", "founder", "cofounder", "co-founder",
    "speedrun", "a16z", "ceo", "cto", "coo",
    "wochen", "monaten", "tagen", "jahren", "reaktionen", "follower",
    "aktualisiert", "let", "linkedin", "post", "posts",
}

# How far around a tag occurrence to look for the company name. Asymmetric
# on purpose: real phrasing overwhelmingly puts the name BEFORE the tag
# ("Company (a16z SR007)"), while text right after the tag is more often
# a location or description ("(SR007) in San Francisco"); see
# nearest_candidate() below, which only falls back to an after-tag match
# when no before-tag candidate exists at all.
WINDOW_BEFORE = 60
WINDOW_AFTER = 80


def is_valid_candidate(candidate: str) -> bool:
    if len(candidate) < 2:
        return False
    words = candidate.lower().split()
    return not any(w in STOP_WORDS for w in words)


def nearest_candidate(text: str, tag_match: re.Match, exclude: str | None = None) -> str | None:
    window_start = max(0, tag_match.start() - WINDOW_BEFORE)
    window_end = min(len(text), tag_match.end() + WINDOW_AFTER)
    window = text[window_start:window_end]
    tag_start_in_window = tag_match.start() - window_start
    tag_end_in_window = tag_match.end() - window_start
    exclude_norm = exclude.strip().lower() if exclude else None

    before, after = [], []
    excluded_before = False
    for name_match in NAME_RE.finditer(window):
        # Skip anything overlapping the tag match itself (prevents the
        # name-capture from ever cannibalizing part of the tag).
        if name_match.end() > tag_start_in_window and name_match.start() < tag_end_in_window:
            continue
        candidate = name_match.group(0).strip()
        if not is_valid_candidate(candidate):
            continue
        is_before = name_match.end() <= tag_start_in_window
        # On short LinkedIn People-search bios, the person's own name (on
        # the card's first line) can land inside this same window and get
        # picked up as if it were the company; confirmed live with
        # "Priya Nair ... a16z SR007" misfiring "Priya Nair" itself as
        # the company. `exclude` lets callers rule that out explicitly
        # rather than tightening the window for every other source too.
        if exclude_norm and candidate.strip().lower() == exclude_norm:
            if is_before:
                excluded_before = True
            continue
        if is_before:
            before.append((tag_start_in_window - name_match.end(), candidate))
        else:
            after.append((name_match.start() - tag_end_in_window, candidate))

    if before:
        return min(before)[1]
    if excluded_before:
        # The ONLY thing sitting before the tag was the person's own name
        # (e.g. "Adrian Zabica ... CTO - a16z speedrun SR007" with no
        # company anywhere in the headline) -- confirmed live that falling
        # through to the after-tag window in this specific situation
        # reliably grabs an unrelated field from a different part of the
        # card (city, school, prior employer: "London", "Harvard Business
        # School", "ex-Microsoft" all fired this way), never the company.
        # Correctly reporting "no company found" here is what lets
        # find_ambiguous_people() route this person to the Google
        # follow-up instead of polluting the roster with a wrong guess.
        return None
    if after:
        return min(after)[1]
    return None


def normalize_company(name: str) -> str:
    n = SUFFIX_RE.sub("", name).strip()
    n = re.sub(r"[^\w\s]", "", n).strip().lower()
    n = re.sub(r"\s+", " ", n)
    return n


def is_noise(text: str) -> bool:
    return any(p.search(text) for p in NOISE_PATTERNS)


def mentions_prior_batch_only(text: str) -> bool:
    has_current = any(p in text for p in BATCH_BIO_PATTERNS) or f"speedrun {CURRENT_BATCH[-3:]}" in text.lower()
    has_prior = any(code in text for code in PRIOR_BATCH_CODES)
    return has_prior and not has_current


def extract_candidates(text: str, source: str, query: str, evidence_url: str = ""):
    if is_noise(text):
        return []
    if mentions_prior_batch_only(text):
        return []
    # Gate on TAG_RE itself, not the narrower BATCH_BIO_PATTERNS list (which
    # requires parens, e.g. "(SR007)"). Confirmed live that real bios skip
    # the parens entirely ("Co-Founder & CEO @ Baro, a16z SR007", no
    # parens) — the old gate silently dropped these before extraction ever
    # got a chance to run, even though nearest_candidate() would have
    # correctly found the company name; e.g. Baro (from your manual
    # LinkedIn screenshot) was present in the raw scrape but never made it
    # onto the roster because of this exact gate, not a search failure.
    if not TAG_RE.search(text):
        return []

    # See nearest_candidate()'s `exclude` param: only relevant for People-
    # search cards, where the person's own name sits right on the card.
    own_name = extract_person_name(text) if source == "linkedin_people_search" else None

    out = []
    seen_names = set()
    for tag_match in TAG_RE.finditer(text):
        raw_name = nearest_candidate(text, tag_match, exclude=own_name)
        if not raw_name or raw_name in seen_names:
            continue
        seen_names.add(raw_name)
        out.append({
            "raw_name": raw_name,
            "normalized_name": normalize_company(raw_name),
            "source": source,
            "query": query,
            "evidence_url": evidence_url,
            "evidence_snippet": text[:400],
            "found_at": datetime.now(timezone.utc).isoformat(),
        })
    return out


def extract_person_name(text: str) -> str | None:
    """
    LinkedIn People-search cards put the person's name on the first
    non-empty line, e.g. "Arnav Bhalla\n• 2nd\nCo-Founder @ Stealth
    Startup (a16z SR007)...". Only called on hits find_ambiguous_people()
    already couldn't extract a company name from, as a name to hand to a
    follow-up Google search instead (see resolve_ambiguous_via_google()).
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        if re.fullmatch(r"•?\s*(1st|2nd|3rd\+?)", line, re.I):
            continue
        return line
    return None


def find_ambiguous_people(text: str, query: str, link: str):
    """
    Companion to extract_candidates(): flags LinkedIn People-search hits
    where the batch tag genuinely matched but no company name could be
    pulled from the card's (often truncated) snippet text -- confirmed
    live that the real company name sometimes only appears on the full
    profile page. This pipeline deliberately does NOT visit profiles to
    check (that's real automated traffic against a real, logged-in
    personal LinkedIn account); instead it returns a person-keyed record
    so resolve_ambiguous_via_google() has a name + link to follow up on
    via an anonymous Google search instead.
    """
    if is_noise(text) or mentions_prior_batch_only(text):
        return None
    tag_matches = list(TAG_RE.finditer(text))
    if not tag_matches:
        return None
    name = extract_person_name(text)
    if any(nearest_candidate(text, m, exclude=name) for m in tag_matches):
        return None  # a real company name WAS found here; not ambiguous
    if not name:
        return None
    return {"person_name": name, "link": link, "snippet": text[:400], "query": query}


def resolve_ambiguous_via_google(ambiguous_people: list[dict]):
    """
    For each ambiguous person, runs ONE targeted Google search on their
    name instead of opening their LinkedIn profile. Opening profiles one
    by one would add real automated browsing activity to a real, logged-in
    personal LinkedIn account -- exactly the kind of pattern that gets
    accounts flagged; Google search is already used elsewhere in this
    pipeline anonymously (no login at all), so this adds zero incremental
    LinkedIn risk. Reuses sr_google_search's driver/CAPTCHA-handling
    rather than duplicating it.

    Returns (resolved_candidates, still_unresolved).
    """
    resolved, unresolved = [], []
    if not ambiguous_people:
        return resolved, unresolved

    import sr_google_search as gsearch

    seen_names = set()
    people = []
    for p in ambiguous_people:
        key = p["person_name"].lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        people.append(p)

    print(f"  {len(people)} unique name(s) need a Google follow-up (no LinkedIn profile visits)...")
    driver = gsearch.build_driver()
    try:
        for person in people:
            query = f'"{person["person_name"]}" "a16z speedrun {CURRENT_BATCH}"'
            try:
                results = gsearch.search_query(driver, query)
            except Exception as e:
                print(f"    WARNING: Google lookup failed for {person['person_name']!r}: {e}")
                results = []

            found_here = []
            for r in results:
                text = f"{r.get('title', '')} {r.get('snippet', '')}"
                found_here += extract_candidates(text, "google_person_lookup", query, r.get("url") or person["link"])

            if found_here:
                resolved.extend(found_here)
            else:
                unresolved.append(person)
            time.sleep(random.uniform(4, 8))
    finally:
        driver.quit()

    return resolved, unresolved


def load_raw_snapshots():
    hits = []
    ambiguous = []
    if not SNAP_DIR.exists():
        return hits, ambiguous
    for f in SNAP_DIR.glob("google_raw_*.json"):
        for r in json.loads(f.read_text()):
            hits += extract_candidates(f"{r.get('title','')} {r.get('snippet','')}", "google", r.get("query", ""), r.get("url", ""))
    for f in SNAP_DIR.glob("li_posts_raw_*.json"):
        for r in json.loads(f.read_text()):
            hits += extract_candidates(r.get("text", ""), "linkedin_post", r.get("query", ""))
    for f in SNAP_DIR.glob("li_people_raw_*.json"):
        for r in json.loads(f.read_text()):
            text = r.get("text", "")
            query = r.get("query", "")
            link = r.get("link", "")
            hits += extract_candidates(text, "linkedin_people_search", query, link)
            amb = find_ambiguous_people(text, query, link)
            if amb:
                ambiguous.append(amb)
    return hits, ambiguous


def merge_into_roster(candidates):
    roster = {}
    if ROSTER_PATH.exists():
        roster = json.loads(ROSTER_PATH.read_text())

    for c in candidates:
        key = c["normalized_name"]
        if not key:
            continue
        if key not in roster:
            roster[key] = {
                "display_name": c["raw_name"],
                "batch": CURRENT_BATCH,
                "first_seen": c["found_at"],
                "evidence": [],
            }
        roster[key]["evidence"].append({
            "source": c["source"],
            "query": c["query"],
            "url": c["evidence_url"],
            "snippet": c["evidence_snippet"],
            "found_at": c["found_at"],
        })

    ROSTER_PATH.write_text(json.dumps(roster, indent=2))
    return roster


def run():
    candidates, ambiguous = load_raw_snapshots()

    resolved, unresolved = [], []
    if ambiguous:
        print(f"{len(ambiguous)} LinkedIn People-search hit(s) matched the batch tag but had no "
              f"extractable company name from the snippet text alone.")
        resolved, unresolved = resolve_ambiguous_via_google(ambiguous)
        candidates += resolved
        if resolved:
            print(f"  Resolved {len(resolved)} via Google.")

    if unresolved:
        UNRESOLVED_PATH.write_text(json.dumps(unresolved, indent=2))
        print(f"  {len(unresolved)} still unresolved after the Google follow-up; "
              f"saved to {UNRESOLVED_PATH} for manual review (name + profile link).")
    elif UNRESOLVED_PATH.exists():
        UNRESOLVED_PATH.unlink()

    roster = merge_into_roster(candidates)
    print(f"Roster now has {len(roster)} provisional {CURRENT_BATCH} companies "
          f"({len(candidates)} new candidate mentions processed this run).")
    return roster


if __name__ == "__main__":
    run()
