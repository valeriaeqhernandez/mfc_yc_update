"""
li_native_search.py

Searches LinkedIn's own search directly (not via Google) for posts
mentioning a YC batch tag; catches announcement posts like Rome Rogers'
"I got into YC F26!" that may not be indexed by Google at all, or not yet.

IMPORTANT: READ BEFORE RUNNING
This drives your REAL, LOGGED-IN LinkedIn session, not an anonymous
browser. That's a meaningfully bigger risk than the Google search script:
LinkedIn's anti-automation detection is more aggressive (see: LinkedIn's
lawsuit against Proxycurl, which shut that company down in 2025), and if
it flags this as automated activity, the consequence lands on your real
account, not a disposable session.

Mitigations built in, but none of these make it risk-free:
  - Uses a PERSISTENT browser profile: you log in manually, once, in a
    real visible window. The script never touches your password directly.
  - No auto-scheduling assumed: treat this as something you run by hand
    occasionally, not an unattended every-12-hours job, at least until
    you've run it several times with no issues.
  - Conservative pacing, single query per run by default.

First-time setup:
    pip install undetected-chromedriver selenium
    python li_native_search.py --login-only
    (a browser window opens: log into LinkedIn manually, then come back
    to the terminal and press Enter. Your session is saved in
    ./li_chrome_profile/ for future runs.)

Usage after first login:
    python li_native_search.py --batch-tag "YC F26"
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

PROFILE_DIR = Path("li_chrome_profile").resolve()
SNAPSHOT_DIR = Path("li_native_snapshots")

# LinkedIn's People search shows ~10 results/page and previously only the
# first page was ever fetched; confirmed live (a manual search turning up
# real candidates the pipeline had missed) that genuine matches commonly
# sit past page 1 for a broad batch tag. Capped rather than unbounded:
# more pages means more automated requests in a row, which is exactly the
# kind of volume this pipeline's pacing is designed to avoid looking like.
MAX_PEOPLE_PAGES = 5


def detect_chrome_major_version():
    """
    Same fix as li_search_selenium.py's version of this function:
    undetected-chromedriver defaults to the globally "latest stable"
    ChromeDriver rather than one matching the Chrome actually installed,
    which breaks session creation whenever Chrome hasn't auto-updated
    yet (confirmed live). Detect the real version and pin to it.
    """
    import undetected_chromedriver as uc

    exe = uc.find_chrome_executable()
    if not exe:
        return None
    try:
        import re
        import subprocess

        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(\d+)\.", out)
        if m:
            return int(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def make_driver():
    """
    Launch Chrome with a PERSISTENT profile directory, so your LinkedIn
    login session is reused across runs instead of logging in fresh each
    time (which would be a much stronger automation signal).
    """
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,900")
    # Deliberately NOT headless: a visible window is lower-detection-risk
    # for this specific job, and this script is meant to be run manually
    # with you present anyway, so there's no headless use case here yet.

    driver = uc.Chrome(options=options, version_main=detect_chrome_major_version())
    return driver


def human_like_delay(a: float = 3.0, b: float = 7.0):
    """
    Wider default range than the Google script; being conservative given
    the higher stakes of automating a logged-in personal account.
    """
    time.sleep(random.uniform(a, b))


def is_checkpoint_page(driver) -> bool:
    """
    Detect LinkedIn's "we've restricted some activity on your account" /
    security-checkpoint pages, so this can be reported clearly instead of
    silently returning nothing.
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


def login_flow():
    """
    One-time interactive login: opens LinkedIn, waits for you to log in
    manually, then confirms the session was saved to the persistent
    profile for future automated runs.
    """
    driver = make_driver()
    driver.get("https://www.linkedin.com/login")
    print(
        "\nA browser window should now be open at LinkedIn's login page.\n"
        "Log in manually (including any 2FA/verification step), then come "
        "back here and press Enter once you're looking at your feed."
    )
    input()

    if "feed" in driver.current_url or "linkedin.com/in/" in driver.current_url:
        print("Looks like login succeeded. Session saved for future runs.")
    else:
        print(
            f"Not fully sure login completed; current URL is "
            f"{driver.current_url}. If future runs fail, re-run --login-only."
        )

    driver.quit()


def handle_checkpoint(driver, interactive: bool = True) -> bool:
    """
    Returns True if it's safe to proceed, False if this query should be
    skipped. In interactive mode (default; a human is at the terminal)
    this pauses for a manual solve. In non-interactive mode (used by
    yc_run_pipeline.py for scheduled/automatic runs, where nobody is
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
            "hanging. Run manually to solve it by hand.",
            file=sys.stderr,
        )
        return False

    print(
        "  LinkedIn showed a checkpoint/verification page instead of "
        "results. Solve it manually in the browser window, then press "
        "Enter to continue."
    )
    input()
    human_like_delay(2, 4)
    if is_checkpoint_page(driver):
        print("  Still on a checkpoint page. Skipping this query.")
        return False
    return True


def run_linkedin_search(driver, query: str, interactive: bool = True) -> list[dict]:
    """
    Search LinkedIn's own "Posts" content search for a query.

    SELECTOR NOTE: LinkedIn's CSS class names are randomized per build
    (hashed, e.g. "_64e7534f"), so unlike the Google script, selecting on
    class names isn't viable at all here.

    Previously anchored on div[data-view-name="feed-full-update"]; a live
    inspection (2026-08-26) found that attribute no longer exists on the
    page AT ALL (0 matches even for the bare attribute, no value filter),
    and this had been silently returning 0 results on every run since at
    least earlier the same day. [role="listitem"] (the same ARIA anchor
    People search already uses) found 6 real, clearly relevant posts on
    the same page in the same test, confirming LinkedIn unified the
    markup between Posts and People search results at some point since
    this selector was first verified. Text extraction within each block
    still relies on structure (first link, surrounding text) rather than
    class names, since those remain unstable.
    """
    from urllib.parse import quote

    url = f"https://www.linkedin.com/search/results/content/?keywords={quote(query)}"
    driver.get(url)
    human_like_delay(5, 9)  # LinkedIn's SPA needs more time to render than a static page

    if not handle_checkpoint(driver, interactive):
        return []

    results = []
    post_blocks = driver.find_elements("css selector", '[role="listitem"]')
    for block in post_blocks:
        try:
            # First profile/company link in the block is reliably the author
            link_el = block.find_element("css selector", "a[href*='/in/'], a[href*='/company/']")
            link = link_el.get_attribute("href")
            author = link_el.text.strip()
        except Exception:
            link = ""
            author = ""

        # Fall back to the block's own text for author name if the link
        # text was empty (LinkedIn sometimes wraps an icon-only <a>)
        text = block.text.strip()
        if not author and text:
            author = text.split("\n")[0]

        if link or text:
            results.append({"author": author, "text": text[:500], "link": link})

    return results


def run_people_search(driver, keywords: str, interactive: bool = True) -> list[dict]:
    """
    Search LinkedIn's People search: finds PROFILES whose headline/bio
    matches the keywords (e.g. "YC F26" in someone's headline), as
    opposed to run_linkedin_search() above which finds POSTS mentioning
    the keywords. This is the "bio_tag" equivalent of the Google/Serper
    query pattern, run against LinkedIn's own search instead.

    SELECTOR NOTE (PARTIALLY UNVERIFIED): I have no saved real HTML for
    LinkedIn's People search results specifically (only the Posts search
    page was inspected together). Rather than guess blind again, this
    reuses role="listitem" as the block anchor: the same real, stable
    (non-hashed) attribute confirmed on the Posts search page; on the
    theory that LinkedIn likely reuses this ARIA pattern across different
    search result list types. This is an informed bet, not a confirmed
    fact. If results come back empty or garbled, we'll do the same
    save-page-source-and-inspect process as before to confirm/fix it.

    PAGINATION: walks LinkedIn's standard ?page=N parameter up to
    MAX_PEOPLE_PAGES, stopping early the first time a page comes back with
    no result blocks (either genuinely out of results, or a results wall).
    """
    from urllib.parse import quote

    # Wrap BARE keywords in explicit quotes to force exact-phrase matching;
    # confirmed necessary after a real test run showed LinkedIn's People
    # search doing loose/OR-ish matching on unquoted multi-word keywords
    # (e.g. "YC F26" unquoted returned YC P26, YC S26, YC W26, and
    # unrelated "YC Alumni" results mixed in). Only wraps if `keywords`
    # doesn't already contain a quote character; multi-clause patterns
    # like '"backed by Y Combinator" "YC F26"' are already exact-phrase
    # quoted per clause, and an extra outer wrap produces malformed syntax
    # that silently returns nothing (confirmed live on the Speedrun
    # pipeline, which had this same bug before it was found and fixed).
    quoted_keywords = keywords if '"' in keywords else f'"{keywords}"'
    encoded_keywords = quote(quoted_keywords)

    results = []
    for page in range(1, MAX_PEOPLE_PAGES + 1):
        url = f"https://www.linkedin.com/search/results/people/?keywords={encoded_keywords}&page={page}"
        driver.get(url)
        human_like_delay(5, 9)

        if not handle_checkpoint(driver, interactive):
            break

        blocks = driver.find_elements("css selector", '[role="listitem"]')
        if not blocks:
            break  # no more results

        page_had_results = False
        for block in blocks:
            try:
                link_el = block.find_element("css selector", "a[href*='/in/']")
                link = link_el.get_attribute("href")
            except Exception:
                continue  # not a person result

            text = block.text.strip()
            if not text:
                continue
            page_had_results = True

            lines = [l for l in text.split("\n") if l.strip()]
            name = lines[0] if lines else ""
            # Headline is typically the next non-connection-degree line
            headline = ""
            for line in lines[1:]:
                if line.strip() not in ("• 1st", "• 2nd", "• 3rd+"):
                    headline = line.strip()
                    break

            results.append({"name": name, "headline": headline, "link": link, "full_text": text[:500]})

        if not page_had_results:
            break
        if page < MAX_PEOPLE_PAGES:
            human_like_delay(3, 6)

    return results


def snapshot_path(query_label: str) -> Path:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in query_label)
    return SNAPSHOT_DIR / f"{safe}.json"


def load_last_snapshot(query_label: str) -> set[str]:
    path = snapshot_path(query_label)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("links", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_snapshot(query_label: str, links: set[str]):
    path = snapshot_path(query_label)
    path.write_text(json.dumps({"links": sorted(links)}, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Search LinkedIn's own search (logged in) for YC batch mentions"
    )
    parser.add_argument("--batch-tag", default="YC F26")
    parser.add_argument(
        "--mode",
        choices=["posts", "people"],
        default="posts",
        help='"posts" finds posts mentioning the batch tag; "people" finds '
        'profiles with the batch tag in their headline/bio',
    )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="Just open LinkedIn for you to log in manually, then save the session and exit",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip (rather than pause for) any LinkedIn checkpoint page; for scheduled/automatic runs only",
    )
    args = parser.parse_args()

    if args.login_only:
        login_flow()
        return

    if not PROFILE_DIR.exists():
        print(
            "ERROR: no saved login profile found. Run this first:\n"
            "  python li_native_search.py --login-only",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        driver = make_driver()
    except Exception as e:
        print(f"ERROR: could not launch Chrome: {e}", file=sys.stderr)
        sys.exit(1)

    query_label = f"{args.mode}_{args.batch_tag}"

    try:
        interactive = not args.non_interactive
        if args.mode == "posts":
            query = f'"{args.batch_tag}" "join"'
            print(f"Searching LinkedIn posts for: {query}")
            results = run_linkedin_search(driver, query, interactive)
            id_key = "link"
            display = lambda r: (r["author"], r.get("text", ""))
        else:
            print(f'Searching LinkedIn people for: "{args.batch_tag}"')
            results = run_people_search(driver, args.batch_tag, interactive)
            id_key = "link"
            display = lambda r: (
                f"{r['name']}: {r['headline']}" if r.get("headline") else r["name"],
                "",
            )

        if not results:
            print(
                "No results parsed. This may mean the selectors need "
                "fixing (see docstrings in run_linkedin_search / "
                "run_people_search) or there genuinely weren't any "
                "matching results."
            )
        else:
            current_ids = {r[id_key] for r in results if r.get(id_key)}
            has_prior_snapshot = snapshot_path(query_label).exists()
            last_known = load_last_snapshot(query_label)
            new_ids = current_ids - last_known if has_prior_snapshot else set()

            for r in results:
                title, text = display(r)
                flag = "  [NEW]" if r.get(id_key) in new_ids else ""
                print(f"\n  - {title}{flag}")
                if text:
                    print(f"      \"{text[:200]}\"")
                print(f"      {r.get(id_key, '')}")

            if has_prior_snapshot:
                print(f"\n{len(new_ids)} new since last run." if new_ids else "\nNo new results since last run.")
            else:
                print("\n(No prior snapshot; treated as baseline.)")

            save_snapshot(query_label, current_ids)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
