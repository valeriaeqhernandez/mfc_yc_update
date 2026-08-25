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

# Same fix as li_native_search.py's MAX_PEOPLE_PAGES: LinkedIn's People
# search shows ~10 results/page and previously only page 1 was ever
# fetched. Confirmed live (a manual LinkedIn search turning up real SR007
# candidates the pipeline had missed entirely) that genuine matches
# commonly sit past page 1. Capped rather than unbounded, since more
# pages means more automated requests in a row.
MAX_PEOPLE_PAGES = 5

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
    """
    Previously waited on and selected [data-view-name="feed-full-update"];
    a live inspection (2026-08-26, same investigation as the fix in
    li_native_search.py's run_linkedin_search()) found that attribute no
    longer exists on the page at all, meaning this was hitting the 12s
    WebDriverWait timeout and returning [] on every single call. Switched
    to [role="listitem"] (the same ARIA anchor People search already
    uses, and confirmed live to contain 6 real, relevant posts on the
    same page in the same test) — LinkedIn evidently unified the markup
    between Posts and People search results since this was first built.
    """
    url = f"https://www.linkedin.com/search/results/content/?keywords={quote(query)}"
    driver.get(url)
    if not handle_checkpoint(driver, interactive):
        return []
    try:
        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="listitem"]'))
        )
    except Exception:
        return []

    time.sleep(2)  # let lazy-loaded content settle
    posts = []
    for el in driver.find_elements(By.CSS_SELECTOR, '[role="listitem"]'):
        text = el.text
        if not text:
            continue
        posts.append({"text": text, "query": query})
    return posts


def search_people(driver, query, interactive: bool = True):
    """
    Wraps a BARE query in explicit quotes before URL-encoding it, same fix
    li_native_search.py's run_people_search() already needed: unquoted
    multi-word keywords make LinkedIn's People search do loose/OR-ish
    matching (confirmed live; an unquoted "(SR007)" query returned
    unrelated 3rd-degree connections with no "SR007" anywhere in their
    profile text), where exact-phrase quoting returns only real matches.

    Only wraps if `query` doesn't already contain a quote character;
    multi-clause patterns like '"joined a16z speedrun" "SR007"' are
    already exact-phrase-quoted per clause, and wrapping THOSE in an
    extra outer pair of quotes produces malformed syntax that silently
    returns nothing rather than erroring. Confirmed live: every pattern
    of that shape returned zero People-search hits until this was fixed,
    while the plain bare patterns worked fine the whole time.

    PAGINATION: walks LinkedIn's standard ?page=N parameter up to
    MAX_PEOPLE_PAGES, stopping early the first time a page comes back
    with no result blocks.
    """
    quoted_query = query if '"' in query else f'"{query}"'
    encoded_query = quote(quoted_query)

    people = []
    for page in range(1, MAX_PEOPLE_PAGES + 1):
        url = f"https://www.linkedin.com/search/results/people/?keywords={encoded_query}&page={page}"
        driver.get(url)
        if not handle_checkpoint(driver, interactive):
            break
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[role="listitem"]'))
            )
        except Exception:
            break  # no more results

        time.sleep(2)
        page_results = []
        for el in driver.find_elements(By.CSS_SELECTOR, '[role="listitem"]'):
            text = el.text
            if not text:
                continue
            # Confirmed live: this was never captured at all, so a hit
            # whose company name couldn't be extracted from the snippet
            # text had nothing left to trace back to; the actual profile
            # was gone with no way to follow up, even manually. Capturing
            # it costs nothing extra (same already-loaded page).
            try:
                link = el.find_element(By.CSS_SELECTOR, "a[href*='/in/']").get_attribute("href")
            except Exception:
                link = ""
            page_results.append({"text": text, "query": query, "link": link})

        if not page_results:
            break
        people.extend(page_results)
        if page < MAX_PEOPLE_PAGES:
            time.sleep(random.uniform(3, 6))

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

    def with_recovery(search_fn, query):
        """
        Runs one search call; on a crashed driver, relaunches once and
        retries this same call before giving up on just this query.
        Confirmed live that a long native-search session (many queries in
        one continuous browser session, now roughly doubled in volume)
        can crash Chrome mid-run ("invalid session id: session deleted as
        the browser has closed the connection"); previously that killed
        data collection for every query after it, including all of
        People search if it happened during Posts. This keeps the rest
        of the run going on a fresh driver instead.
        """
        nonlocal driver
        try:
            return search_fn(driver, query, interactive)
        except Exception as e:
            print(f"    WARNING: query failed ({e}); relaunching Chrome and retrying once...", file=sys.stderr)
            try:
                driver.quit()
            except Exception:
                pass
            try:
                driver = build_driver()
                return search_fn(driver, query, interactive)
            except Exception as e2:
                print(f"    WARNING: retry also failed ({e2}); skipping this query.", file=sys.stderr)
                return []

    try:
        for q in SEARCH_QUERIES:
            print(f"[posts] {q}")
            all_posts.extend(with_recovery(search_posts, q))
            time.sleep(random.uniform(3, 7))

        for q in LINKEDIN_PEOPLE_SEARCH_QUERIES:
            print(f"[people] {q}")
            all_people.extend(with_recovery(search_people, q))
            time.sleep(random.uniform(3, 7))
    finally:
        try:
            driver.quit()
        except Exception:
            pass

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
