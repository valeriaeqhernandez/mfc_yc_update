"""
sr_li_native_search.py: adapted from li_native_search.py

Searches LinkedIn Posts and People natively via a persistent logged-in
Chrome profile (same auth approach as the YC pipeline; no LinkedIn API
usage, since LinkedIn's official API doesn't expose this kind of search
either).

Unchanged from YC pipeline:
  - LinkedIn's CSS class names are fully randomized per build; only
    structural selectors survive across sessions:
      * post containers: [data-view-name="feed-full-update"]
      * people-search result rows: [role="listitem"]
  - Uses the SAME persistent Chrome profile dir as li_native_search.py
    (./li_chrome_profile/), not a separate one; it's the same LinkedIn
    account either way, so this reuses the existing logged-in session
    instead of requiring a second manual login. If that directory doesn't
    exist yet, run `python li_native_search.py --login-only` first.

Changed from YC pipeline:
  - Two search surfaces matter here, not one: LinkedIn Posts (founders
    announcing) AND LinkedIn People search on headline text (people who've
    already updated their title to "... (SR007)" before/without posting
    about it). The People-search surface is arguably higher signal for
    Speedrun specifically, since acceptance is rolling and a title update
    often precedes any public post.
"""

import json
import re
import subprocess
import sys
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from sr_queries import SEARCH_QUERIES, LINKEDIN_PEOPLE_SEARCH_QUERIES, CURRENT_BATCH

PROFILE_DIR = Path("li_chrome_profile").resolve()
OUT_DIR = Path("snapshots") / CURRENT_BATCH
OUT_DIR.mkdir(parents=True, exist_ok=True)

def detect_chrome_major_version():
    """
    Same fix as sr_google_search.py: undetected-chromedriver defaults to
    the globally "latest stable" ChromeDriver rather than one matching the
    Chrome actually installed, which breaks session creation whenever
    Chrome hasn't auto-updated yet. Detect the real version and pin to it.

    Uses uc's own find_chrome_executable() (macOS/Linux/Windows) instead
    of a hardcoded path, so it resolves correctly on whoever's machine
    runs this script.
    """
    exe = uc.find_chrome_executable()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(\d+)\.", out)
        if m:
            return int(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def build_driver():
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--window-size=1280,900")
    driver = uc.Chrome(
        options=options, use_subprocess=True, version_main=detect_chrome_major_version()
    )
    return driver


def is_checkpoint_page(driver) -> bool:
    """
    Same check as li_native_search.py's is_checkpoint_page(); carried
    over here since it was missing (this script previously just returned
    an empty result set with no explanation whenever LinkedIn showed a
    security-checkpoint page, instead of reporting what actually happened).
    """
    url_lower = driver.current_url.lower()
    source_lower = driver.page_source.lower()
    signals = [
        "checkpoint" in url_lower,
        "authwall" in url_lower,
        "unusual activity" in source_lower,
        "verify" in url_lower and "linkedin" in url_lower,
    ]
    return any(signals)


def handle_checkpoint(driver, interactive: bool) -> bool:
    """
    Returns True if it's now safe to proceed, False if this query should
    be skipped. In interactive mode (a human is at the terminal; the
    default, and what every manual run uses) this pauses for a manual
    solve, same as li_native_search.py. In non-interactive mode (used by
    sr_run_pipeline.py for scheduled/automatic runs, where nobody is
    watching) it does NOT call input(); that would hang the process
    indefinitely, turning one blocked query into a stuck scheduled job
    that never runs again. It logs and skips instead.
    """
    if not is_checkpoint_page(driver):
        return True

    if not interactive:
        print(
            "  LinkedIn showed a checkpoint/verification page during a "
            "non-interactive run; skipping this query rather than "
            "hanging. Run manually (python sr_li_native_search.py) to "
            "solve it by hand.",
            file=sys.stderr,
        )
        return False

    print(
        "  LinkedIn showed a checkpoint/verification page instead of "
        "results. Solve it manually in the browser window, then press "
        "Enter to continue."
    )
    input()
    time.sleep(random.uniform(2, 4))
    if is_checkpoint_page(driver):
        print("  Still on a checkpoint page. Skipping this query.")
        return False
    return True


def search_posts(driver, query, interactive: bool = True):
    url = f"https://www.linkedin.com/search/results/content/?keywords={quote(query)}"
    driver.get(url)
    if not handle_checkpoint(driver, interactive):
        return []
    try:
        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[data-view-name="feed-full-update"]'))
        )
    except Exception:
        return []

    time.sleep(2)  # let lazy-loaded content settle
    posts = []
    for el in driver.find_elements(By.CSS_SELECTOR, '[data-view-name="feed-full-update"]'):
        text = el.text
        if not text:
            continue
        posts.append({"text": text, "query": query})
    return posts


def search_people(driver, query, interactive: bool = True):
    """
    Wraps the query in explicit quotes before URL-encoding it, same fix
    li_native_search.py's run_people_search() already needed: unquoted
    multi-word keywords make LinkedIn's People search do loose/OR-ish
    matching (confirmed live; an unquoted "(SR007)" query returned
    unrelated 3rd-degree connections with no "SR007" anywhere in their
    profile text), where exact-phrase quoting returns only real matches.
    """
    quoted_query = f'"{query}"'
    url = f"https://www.linkedin.com/search/results/people/?keywords={quote(quoted_query)}"
    driver.get(url)
    if not handle_checkpoint(driver, interactive):
        return []
    try:
        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="listitem"]'))
        )
    except Exception:
        return []

    time.sleep(2)
    people = []
    for el in driver.find_elements(By.CSS_SELECTOR, '[role="listitem"]'):
        text = el.text
        if not text:
            continue
        people.append({"text": text, "query": query})
    return people


def run(interactive: bool = True):
    if not PROFILE_DIR.exists():
        print(
            "ERROR: no saved LinkedIn login profile found at "
            f"{PROFILE_DIR}. Run this first:\n"
            "  python li_native_search.py --login-only",
            file=sys.stderr,
        )
        sys.exit(1)

    driver = build_driver()
    all_posts, all_people = [], []
    try:
        for q in SEARCH_QUERIES:
            print(f"[posts] {q}")
            all_posts.extend(search_posts(driver, q, interactive))
            time.sleep(random.uniform(3, 7))

        for q in LINKEDIN_PEOPLE_SEARCH_QUERIES:
            print(f"[people] {q}")
            all_people.extend(search_people(driver, q, interactive))
            time.sleep(random.uniform(3, 7))
    finally:
        driver.quit()

    today = datetime.now(timezone.utc).date().isoformat()
    (OUT_DIR / f"li_posts_raw_{today}.json").write_text(json.dumps(all_posts, indent=2))
    (OUT_DIR / f"li_people_raw_{today}.json").write_text(json.dumps(all_people, indent=2))
    print(f"Wrote {len(all_posts)} posts, {len(all_people)} people to {OUT_DIR}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Search LinkedIn (posts + people) for SR007 mentions")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip (rather than pause for) any LinkedIn checkpoint page; for scheduled/automatic runs only",
    )
    args = parser.parse_args()
    run(interactive=not args.non_interactive)
