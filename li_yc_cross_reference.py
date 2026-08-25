"""
li_yc_cross_reference.py

Combines yc_list_raw.py (YC's own public directory) and
li_search_selenium.py (LinkedIn-via-Google search) to answer the actual
question that matters: of the companies/founders showing up on LinkedIn
claiming a YC batch, which ones are genuinely NOT YET on YC's public
directory (real pre-MFN leads) versus already publicly listed (confirmed,
but no longer early)?

This is the fix for the false-positive problem we hit earlier with
RunInfra/RightNow: a LinkedIn hit alone isn't enough to know if it's a
fresh lead or something already public.

Requires yc_list_raw.py and li_search_selenium.py in the same folder.

Usage:
    python li_yc_cross_reference.py --batch-tag "YC F26" --yc-batch-slug fall-2026
"""

import argparse
import re
import sys

import yc_list_raw as yc
import li_search_selenium as li


def normalize(name: str) -> str:
    """Lowercase, strip punctuation, for loose substring matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_matching_yc_company(li_result: dict, yc_companies: list[dict]) -> dict | None:
    """
    Check whether a LinkedIn result's title+snippet mentions a company
    name that's already in the YC directory list. Uses normalized
    substring matching rather than exact match, since LinkedIn titles are
    messy ("Jamie Yau - Co-Founder @ Collar (YC F26) The AI ...").

    This is intentionally simple (no fuzzy/edit-distance matching): good
    enough to catch clear cases like "Collar" appearing in both, but can
    miss company names that are very short/generic (more false negatives
    than false positives, which is the safer failure mode here: a missed
    match just means a real hit shows up as "not yet listed" when it
    actually is; worth a human glancing at it either way).
    """
    haystack = normalize(li_result["title"] + " " + li_result.get("snippet", ""))
    for company in yc_companies:
        company_name_norm = normalize(company["name"])
        if len(company_name_norm) >= 3 and company_name_norm in haystack:
            return company
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Cross-reference LinkedIn YC-batch mentions against YC's public directory"
    )
    parser.add_argument("--batch-tag", default="YC F26", help='e.g. "YC F26"')
    parser.add_argument(
        "--yc-batch-slug",
        default="fall-2026",
        help='e.g. "fall-2026"; must match the batch-tag season/year',
    )
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()

    # --- Step 1: fetch YC's current public directory for this batch ---
    print(f"Fetching YC directory for batch: {args.yc_batch_slug}")
    try:
        yc_companies = yc.fetch_batch_companies(args.yc_batch_slug)
    except Exception as e:
        print(f"ERROR: could not fetch YC directory: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(yc_companies)} companies currently public on YC's directory.")

    # --- Step 2: run the LinkedIn searches ---
    try:
        driver = li.make_driver(headless=not args.headful)
    except Exception as e:
        print(f"ERROR: could not launch Chrome: {e}", file=sys.stderr)
        sys.exit(1)

    all_li_results = []
    try:
        print("\nWarming up before searching...")
        li.warm_up(driver)

        for label, template in li.QUERY_TEMPLATES:
            query = template.format(batch_tag=args.batch_tag)
            print(f"\n=== Query [{label}]: {query} ===")
            results = li.run_google_search(
                driver, query, allow_manual_captcha_solve=args.headful
            )
            print(f"  {len(results)} result(s).")
            all_li_results.extend(results)
            li.human_like_delay(6, 14)
    finally:
        driver.quit()

    # --- Step 3: cross-reference ---
    # Dedupe LinkedIn results by URL, since the two queries often overlap
    seen_urls = set()
    deduped_results = []
    for r in all_li_results:
        if r["link"] not in seen_urls:
            seen_urls.add(r["link"])
            deduped_results.append(r)

    print(f"\n{'=' * 60}")
    print(f"CROSS-REFERENCE: {len(deduped_results)} unique LinkedIn result(s)")
    print(f"{'=' * 60}\n")

    pre_mfn_leads = []
    already_listed = []

    for r in deduped_results:
        match = find_matching_yc_company(r, yc_companies)
        if match:
            already_listed.append((r, match))
        else:
            pre_mfn_leads.append(r)

    print(f">>> {len(pre_mfn_leads)} NOT YET on YC's public directory (potential pre-MFN leads) <<<\n")
    for r in pre_mfn_leads:
        print(f"  - {r['title']}")
        print(f"      {r['link']}")
        if r.get("snippet"):
            print(f"      \"{r['snippet']}\"")
        print()

    print(f"\n>>> {len(already_listed)} already on YC's public directory (confirmed, no longer pre-MFN) <<<\n")
    for r, match in already_listed:
        print(f"  - {r['title']}  →  matched YC company: {match['name']}")
        print(f"      {r['link']}")
        print()

    if not pre_mfn_leads:
        print("(No genuinely new pre-MFN leads this run; everything found is already publicly listed.)")


if __name__ == "__main__":
    main()
