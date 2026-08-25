"""
yc_list_raw.py

Bare-bones script: fetches the current list of companies in a given YC
batch and prints it to the terminal. No Google Sheets, no email; just
getting the list right first.

Data source: https://github.com/yc-oss/api; an open-source project that
reads YC's own public Algolia search index (the same backend that powers
the search box on ycombinator.com/companies), rather than scraping the
page's HTML. This matters because YC's robots.txt explicitly disallows
automated access to the /companies page itself; this endpoint sidesteps
that entirely since it never touches ycombinator.com.

Usage:
    python yc_list_raw.py
    python yc_list_raw.py --batch winter-2026
"""

import argparse
import json
import sys
from pathlib import Path

import requests

API_BASE = "https://yc-oss.github.io/api/batches"

SNAPSHOT_DIR = Path("yc_snapshots")  # one file per batch, no external service


def snapshot_path(batch_slug: str) -> Path:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    return SNAPSHOT_DIR / f"{batch_slug}.json"


def load_last_snapshot(batch_slug: str) -> set[str]:
    path = snapshot_path(batch_slug)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("company_names", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_snapshot(batch_slug: str, company_names: set[str]):
    path = snapshot_path(batch_slug)
    path.write_text(json.dumps({"company_names": sorted(company_names)}, indent=2))


def fetch_batch_companies(batch_slug: str) -> list[dict]:
    """
    Fetch the current company list for a given batch.

    batch_slug format: lowercase, hyphenated, e.g. "fall-2026", "winter-2026".
    """
    url = f"{API_BASE}/{batch_slug}.json"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description="List current YC batch companies")
    parser.add_argument(
        "--batch",
        default="fall-2026",
        help="Batch slug, e.g. fall-2026, winter-2026 (default: fall-2026)",
    )
    args = parser.parse_args()
    batch_slug = args.batch

    print(f"Fetching batch: {batch_slug}")
    try:
        companies = fetch_batch_companies(batch_slug)
    except requests.HTTPError as e:
        print(f"ERROR: could not fetch batch '{batch_slug}': {e}", file=sys.stderr)
        print(
            "Double-check the batch slug format (lowercase-hyphenated, "
            "e.g. 'fall-2026', not 'Fall 2026').",
            file=sys.stderr,
        )
        sys.exit(1)

    if not companies:
        print(f"No companies returned for batch '{batch_slug}'.")
        return

    current_names = {c["name"] for c in companies}
    has_prior_snapshot = snapshot_path(batch_slug).exists()
    last_known = load_last_snapshot(batch_slug)
    new_names = sorted(current_names - last_known) if has_prior_snapshot else []

    print(f"\n{len(companies)} companies currently listed in {batch_slug}:\n")
    for c in sorted(companies, key=lambda c: c["name"]):
        flag = "  [NEW]" if c["name"] in new_names else ""
        print(f"  - {c['name']:<20} {c['one_liner']}{flag}")
        print(f"      {c['url']}")

    if has_prior_snapshot:
        if new_names:
            print(f"\n{len(new_names)} new since last run: {', '.join(new_names)}")
        else:
            print("\nNo new companies since last run.")
    else:
        print(
            "\n(No prior snapshot found for this batch; this is treated as the "
            "baseline. Run again later to see new companies flagged as [NEW].)"
        )

    save_snapshot(batch_slug, current_names)


if __name__ == "__main__":
    main()