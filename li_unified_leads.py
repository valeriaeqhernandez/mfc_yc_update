"""
li_unified_leads.py

Combines all three lead-finding sources built so far into one run:
  1. Google/Serum-style search via Selenium (li_search_selenium.py)
     -> two sub-sources: "google_bio_tag" and "google_announcement"
  2. LinkedIn's own Posts search, logged in (li_native_search.py)
     -> source: "linkedin_post"
  3. LinkedIn's own People search, logged in (li_native_search.py)
     -> source: "linkedin_people"

Each lead is normalized into a common structure with a SOURCE field, so
a future dashboard/visual can show "found via: LinkedIn People search"
etc. Designed to be extensible: adding a Twitter source later means
adding one more normalized-lead-producing function, same shape.

Every lead is cross-referenced against YC's public directory
(yc_list_raw.py) to flag which are genuinely NOT YET listed (pre-MFN
candidates) vs already public.

Where possible, a founder/person NAME is extracted and stored separately
from the raw title/headline text.

Output:
  - Printed summary to terminal, grouped by pre-MFN vs already-listed
  - A JSON file (leads_<batch>.json) with full structured data per lead,
    intended as the data source for a future visual/dashboard

Requires yc_list_raw.py, li_search_selenium.py, and li_native_search.py
in the same folder.

Usage:
    python li_unified_leads.py --batch-tag "YC F26" --yc-batch-slug fall-2026
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yc_list_raw as yc
import li_search_selenium as gsel
import li_native_search as lnat
from yc_queries import BASE_PATTERNS, LINKEDIN_PEOPLE_PATTERNS


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


COMPANY_SUFFIXES = ("inc", "ai", "labs", "co", "llc", "corp", "technologies", "tech")


def normalize_core(name: str) -> str:
    """
    Strip a trailing common corporate suffix after normalizing, so
    "Degla Inc" and "Degla AI" both reduce to "degla" and match each
    other. Only strips if the remaining core is still >= 3 chars, to
    avoid mangling short names.

    Real bug this fixes: YC's directory listed "Degla Inc" while the
    LinkedIn mention said "Degla AI" -- same company, different suffix --
    and plain substring matching correctly refused to force a match
    between two different strings (the right conservative default), but
    meant a real match got missed.
    """
    n = normalize(name)
    for suffix in COMPANY_SUFFIXES:
        if n.endswith(suffix) and len(n) - len(suffix) >= 3:
            return n[: -len(suffix)]
    return n


def find_matching_yc_company(text: str, yc_companies: list[dict]) -> dict | None:
    haystack = normalize(text)
    for company in yc_companies:
        # Checking the company's CORE name (suffix stripped) against the
        # raw haystack is sufficient on its own -- if the full name would
        # have matched, its core (a prefix of it) matches too.
        company_core = normalize_core(company["name"])
        if len(company_core) >= 3 and company_core in haystack:
            return company
    return None


def guess_company_from_text(text: str) -> str | None:
    """
    Best-effort extraction of a company name from messy title/headline
    text, e.g. "Co-Founder @ Collar (YC F26)" -> "Collar", or
    "Building Fenn AI (YC F26)" -> "Fenn AI".

    This is a heuristic, not a reliable parser; LinkedIn text is too
    varied for a single regex to catch everything. Treat extracted names
    as a helpful starting guess to verify by eye, not ground truth.

    Case-insensitive throughout (an earlier version missed lowercase
    headlines like "co-founder @ microsandbox (yc f26)"; caught by
    testing against real examples from this conversation before shipping).
    """
    patterns = [
        # "Co-Founder & CTO at ClaimKit", "Founding AI Engineer @ Simantic"
        # allows a few extra role words between the founder/founding
        # keyword and the @/at/of preposition. Uses [^@()]{0,25}? rather
        # than \b...\b for the gap: an earlier attempt with \b broke on
        # "Co-Founder @ Collar" because there's no word-boundary between
        # a space and "@" (both non-word characters); caught by
        # re-running the same test cases after the first fix, which is
        # exactly why re-testing after every change matters here.
        r"(?:co-?founder|founding\s+[a-z]+(?:\s+[a-z]+){0,2})[^@()]{0,25}?(?:@|\bat\b|\bof\b)\s*([A-Za-z0-9&.\-' ]+?)\s*\(YC",
        r"building\s+([A-Za-z0-9&.\-' ]+?)\s*\(YC",
        # General fallback for any OTHER role, not just founder/founding
        # e.g. "Rachel Huang - CEO @ ClaimKit (YC F26)". Added after a
        # real example (a CEO, not a co-founder) was missed by the
        # founder-specific pattern above.
        r"@\s*([A-Za-z0-9&.\-' ]+?)\s*\(YC",
        # Fallback: leading "CompanyName (YC ...)" with nothing else before
        # it; deliberately excludes text containing " - " or "@" earlier,
        # since those cases are a "Name - Role @ Company" line that the
        # patterns above should be handling instead.
        r"^(?!.*\s-\s)(?!.*@)([A-Za-z0-9&.\-' ]+?)\s*\(YC\s",
        # Last-resort, lower-confidence fallback: "@ Company" with NO
        # nearby "(YC" requirement at all -- needed for cases like a
        # LinkedIn People-search result whose captured headline text
        # doesn't itself contain the batch tag (it's elsewhere on their
        # full profile). Real example this fixes: Aziz Hanafi / Degla AI
        # ("Building @ Degla AI | Computer Science @ MIT" has no "(YC"
        # anywhere in the captured text).
        r"@\s*([A-Za-z0-9&.\-' ]+?)(?:\s*\||\s*\(|$)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def guess_name_from_text(text: str) -> str | None:
    """
    First non-boilerplate line of a title/text block is very often the
    person's name (e.g. "Jamie Yau - Co-Founder @ Collar (YC F26)").
    Strips a trailing " - <role>" if present.

    Skips known LinkedIn UI chrome labels that can appear as the literal
    first line of block.text (e.g. "Feed post"); caught by testing
    against a real run where three leads incorrectly showed "Feed post"
    as their name instead of the actual author.
    """
    UI_CHROME_LABELS = {"feed post", "post", "reposted", "promoted"}

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        if line.lower() not in UI_CHROME_LABELS:
            first_line = line
            break
    else:
        return None

    for sep in (" - ", " – "):
        if sep in first_line:
            return first_line.split(sep)[0].strip()
    return first_line


def make_lead(source: str, name: str | None, raw_text: str, link: str, yc_companies: list[dict]) -> dict:
    company_guess = guess_company_from_text(raw_text)
    yc_match = find_matching_yc_company(raw_text, yc_companies)
    return {
        "name": name or guess_name_from_text(raw_text),
        "company_guess": company_guess,
        "source": source,
        "link": link,
        "snippet": raw_text[:400],
        "yc_match": yc_match["name"] if yc_match else None,
        "pre_mfn": yc_match is None,
    }


def dedupe_leads(leads: list[dict]) -> list[dict]:
    """
    Merge leads that point to the same LinkedIn URL (found by multiple
    sources), combining their source tags into one list rather than
    keeping duplicate entries.
    """
    by_link: dict[str, dict] = {}
    for lead in leads:
        link = lead["link"]
        if not link:
            continue
        if link not in by_link:
            merged = dict(lead)
            merged["sources"] = [lead["source"]]
            del merged["source"]
            by_link[link] = merged
        else:
            existing = by_link[link]
            if lead["source"] not in existing["sources"]:
                existing["sources"].append(lead["source"])
            # Fill in any missing fields from this duplicate sighting
            if not existing.get("name") and lead.get("name"):
                existing["name"] = lead["name"]
            if not existing.get("company_guess") and lead.get("company_guess"):
                existing["company_guess"] = lead["company_guess"]
    return list(by_link.values())


def run(batch_tag: str, yc_batch_slug: str, interactive: bool = True):
    """
    Everything main() used to do inline, factored out so
    yc_run_pipeline.py can call this directly instead of shelling out.
    `interactive` controls whether a Google CAPTCHA or LinkedIn checkpoint
    pauses for a manual solve (default, for a human-run session) or gets
    skipped with a log message (for scheduled/automatic runs, where
    blocking on input() would hang the job forever with nobody there to
    unblock it).
    """
    print(f"Fetching YC directory for batch: {yc_batch_slug}")
    try:
        yc_companies = yc.fetch_batch_companies(yc_batch_slug)
    except Exception as e:
        print(f"ERROR: could not fetch YC directory: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(yc_companies)} companies currently public.\n")

    all_leads: list[dict] = []

    # --- Source 1 & 2: Google search (anonymous browser) ---
    print("=== Source: Google search (anonymous) ===")
    # Retries once: a real run showed a startup crash ("no such window:
    # target window already closed") that did NOT reproduce when running
    # li_search_selenium.py standalone immediately after; pointing to an
    # intermittent Chrome startup issue rather than a real code bug, so a
    # simple retry is the pragmatic fix rather than something deeper.
    google_source_ok = False
    for attempt in (1, 2):
        try:
            gdriver = gsel.make_driver(headless=False)
            gsel.warm_up(gdriver)
            for label, template in gsel.QUERY_TEMPLATES:
                query = template.format(batch_tag=batch_tag)
                source_tag = f"google_{label}"
                print(f"  Query [{source_tag}]: {query}")
                results = gsel.run_google_search(gdriver, query, allow_manual_captcha_solve=interactive)
                print(f"    {len(results)} result(s)")
                for r in results:
                    all_leads.append(
                        make_lead(source_tag, None, f"{r['title']}\n{r.get('snippet', '')}", r["link"], yc_companies)
                    )
                gsel.human_like_delay(6, 14)
            gdriver.quit()
            google_source_ok = True
            break
        except Exception as e:
            print(f"  WARNING: Google search source failed (attempt {attempt}/2): {e}", file=sys.stderr)
            try:
                gdriver.quit()
            except Exception:
                pass
            if attempt == 1:
                import time as _time
                print("  Retrying once after a short pause...")
                _time.sleep(5)

    if not google_source_ok:
        print("  Google search source failed on both attempts; continuing with LinkedIn sources only.")

    # Give Chrome a moment to fully release before starting the second,
    # separate session (LinkedIn) below.
    import time as _time
    _time.sleep(3)

    # --- Source 3 & 4: LinkedIn native Posts + People search (logged in) ---
    print("\n=== Source: LinkedIn Posts search (logged in) ===")
    if not lnat.PROFILE_DIR.exists():
        print("  SKIPPED: no saved LinkedIn login profile. Run li_native_search.py --login-only first.")
    else:
        try:
            ldriver = lnat.make_driver()
        except Exception as e:
            print(f"  WARNING: could not launch Chrome for LinkedIn native search: {e}", file=sys.stderr)
            ldriver = None

        if ldriver is not None:
            def with_recovery(search_fn, query):
                """
                Runs one search call; on a crashed driver, relaunches once
                and retries this same call before giving up on just this
                query. Confirmed live that a long native-search session
                (many queries in one continuous browser session, now
                roughly doubled in volume) can crash Chrome mid-run
                ("invalid session id: session deleted as the browser has
                closed the connection"); previously that killed data
                collection for every query after it, including all of
                People search if the crash happened during Posts. This
                keeps the rest of the run going on a fresh driver instead.
                """
                nonlocal ldriver
                try:
                    return search_fn(ldriver, query, interactive)
                except Exception as e:
                    print(f"    WARNING: query failed ({e}); relaunching Chrome and retrying once...", file=sys.stderr)
                    try:
                        ldriver.quit()
                    except Exception:
                        pass
                    try:
                        ldriver = lnat.make_driver()
                        return search_fn(ldriver, query, interactive)
                    except Exception as e2:
                        print(f"    WARNING: retry also failed ({e2}); skipping this query.", file=sys.stderr)
                        return []

            for label, pattern in BASE_PATTERNS:
                query = pattern.format(batch_tag=batch_tag)
                print(f"  Query [{label}]: {query}")
                results = with_recovery(lnat.run_linkedin_search, query)
                print(f"    {len(results)} result(s)")
                for r in results:
                    all_leads.append(
                        make_lead("linkedin_post", r.get("author"), r.get("text", ""), r.get("link", ""), yc_companies)
                    )
                lnat.human_like_delay(3, 6)

            # --- Source 4: LinkedIn native People search (same session) ---
            print("\n=== Source: LinkedIn People search (logged in) ===")
            for label, pattern in LINKEDIN_PEOPLE_PATTERNS:
                query = pattern.format(batch_tag=batch_tag)
                print(f"  Query [{label}]: {query}")
                people_results = with_recovery(lnat.run_people_search, query)
                print(f"    {len(people_results)} result(s)")
                for r in people_results:
                    combined_text = f"{r['name']}\n{r.get('headline', '')}"
                    all_leads.append(
                        make_lead("linkedin_people", r.get("name"), combined_text, r.get("link", ""), yc_companies)
                    )
                lnat.human_like_delay(3, 6)

            try:
                ldriver.quit()
            except Exception:
                pass

    # --- Merge, dedupe, report ---
    leads = dedupe_leads(all_leads)
    pre_mfn = [l for l in leads if l["pre_mfn"]]
    already_listed = [l for l in leads if not l["pre_mfn"]]

    print(f"\n{'=' * 60}")
    print(f"UNIFIED RESULT: {len(leads)} unique leads across all sources")
    print(f"{'=' * 60}\n")

    print(f">>> {len(pre_mfn)} PRE-MFN candidates (not yet on YC directory) <<<\n")
    for l in pre_mfn:
        print(f"  - {l['name'] or '(name unknown)'}" + (f"  [company guess: {l['company_guess']}]" if l["company_guess"] else ""))
        print(f"      sources: {', '.join(l['sources'])}")
        print(f"      {l['link']}")
        print()

    print(f"\n>>> {len(already_listed)} already on YC directory <<<\n")
    for l in already_listed:
        print(f"  - {l['name'] or '(name unknown)'}  →  {l['yc_match']}")
        print(f"      sources: {', '.join(l['sources'])}")
        print(f"      {l['link']}")
        print()

    # --- Write structured JSON for future visual/dashboard use ---
    output = {
        "batch_tag": batch_tag,
        "yc_batch_slug": yc_batch_slug,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "yc_directory_count": len(yc_companies),
        "leads": leads,
    }
    out_path = Path(f"leads_{yc_batch_slug}.json")
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nStructured data written to {out_path}")
    return output


def main():
    parser = argparse.ArgumentParser(description="Unified multi-source YC lead finder")
    parser.add_argument("--batch-tag", default="YC F26")
    parser.add_argument("--yc-batch-slug", default="fall-2026")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip (rather than pause for) any Google CAPTCHA or LinkedIn checkpoint; for scheduled/automatic runs only",
    )
    args = parser.parse_args()
    run(args.batch_tag, args.yc_batch_slug, interactive=not args.non_interactive)


if __name__ == "__main__":
    main()
