"""
li_search_selenium.py

Alternative to li_search_raw.py (Serper.dev): uses a real, undetected
Chrome browser via Selenium to run Google searches directly, instead of
paying a SERP API. Free, but more fragile: Google's result page markup
changes periodically and isn't guaranteed to match the selectors below;
see the note at the bottom before relying on this.

Uses `undetected-chromedriver` rather than plain Selenium specifically
because vanilla Selenium sets JS-detectable automation flags (e.g.
navigator.webdriver = true) that Google's anti-bot systems are built to
catch. undetected-chromedriver patches these out. This reduces detection
risk but does not eliminate it; see the conversation notes on why a
personal-scale, twice-daily search is lower risk than high-volume scraping,
but not zero risk.

Setup:
    pip install undetected-chromedriver selenium

    You need a real Chrome or Chromium browser installed on your machine
    (not just the Python packages); undetected-chromedriver drives an
    actual browser install, it doesn't bundle one.

Usage:
    python li_search_selenium.py
    python li_search_selenium.py --batch-tag "YC W26"
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

QUERY_TEMPLATES = [
    ('bio_tag', 'site:linkedin.com "Co-founder" "{batch_tag}"'),
    ('announcement', 'site:linkedin.com "{batch_tag}" "Y Combinator"'),
]

SNAPSHOT_DIR = Path("li_snapshots_selenium")


def make_driver(headless: bool = True):
    """
    Launch an undetected Chrome instance.

    NOTE on headless: headless mode is itself a weak detection signal (it's
    easier for anti-bot systems to spot than a real windowed browser). If
    you're running this on a machine where you can leave a visible Chrome
    window running in the background, setting headless=False is the more
    "looks like a real person" option; with the natural tradeoff that a
    scheduled/unattended job usually needs headless mode to run without a
    visible window. Start with headless=True for a scheduled job; if you
    hit CAPTCHA walls quickly, this tradeoff is the first thing to revisit.
    """
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,900")

    driver = uc.Chrome(options=options)
    return driver


def human_like_delay(a: float = 1.5, b: float = 4.0):
    """Randomized pause, since perfectly uniform timing is itself a signal."""
    time.sleep(random.uniform(a, b))


def is_captcha_page(driver) -> bool:
    """
    Detect whether Google served a CAPTCHA/"unusual traffic" challenge
    instead of real results, so we can report this clearly rather than
    silently returning zero results and leaving you to guess why.
    """
    source_lower = driver.page_source.lower()
    title_lower = driver.title.lower()
    signals = [
        "recaptcha" in source_lower,
        "g-recaptcha" in source_lower,
        "unusual traffic" in source_lower,
        "sorry" in title_lower and "google" in title_lower,
    ]
    return any(signals)


def warm_up(driver):
    """
    Visit Google's homepage and pause before running the first real search,
    rather than jumping straight to a search-results URL cold. A real
    person opening a browser and searching doesn't teleport directly to a
    results page; this at least gives the session a slightly more
    plausible-looking navigation history before the first query.
    """
    driver.get("https://www.google.com")
    human_like_delay(4, 8)


def run_google_search(driver, query: str, allow_manual_captcha_solve: bool = False) -> list[dict]:
    """
    Load a Google search results page and extract organic results.

    If a CAPTCHA is detected and allow_manual_captcha_solve is True (i.e.
    running --headful), this pauses and waits for you to solve it in the
    visible browser window, then continues automatically once you press
    Enter. In headless mode there's no window to solve it in, so it just
    reports the CAPTCHA and returns no results.

    IMPORTANT: SELECTORS CONFIRMED AGAINST REAL DATA. Unlike the first
    version of this function, these selectors were verified against an
    actual saved Google results page (div[data-rpos] as the per-result
    container, div.yuRUbf a[href] for the title link, div.VwiC3b for the
    snippet); not guessed from documentation. Google can still change
    these at any time without notice; if results start coming back empty
    again later, that's the first thing to re-check the same way we did
    the first time (save page_source, inspect the real HTML).
    """
    url = f"https://www.google.com/search?q={query}"
    driver.get(url)
    human_like_delay()

    if is_captcha_page(driver):
        if allow_manual_captcha_solve:
            print(
                "\n  >>> CAPTCHA detected. A browser window should be open; "
                "solve it manually, then press Enter here to continue. <<<"
            )
            input()
            human_like_delay(2, 4)
            if is_captcha_page(driver):
                print("  Still showing a CAPTCHA/challenge page after solving. Skipping this query.")
                return []
        else:
            print(
                "  CAPTCHA/challenge page detected (running headless, so it "
                "can't be solved manually). Re-run with --headful to solve "
                "it by hand."
            )
            return []

    results = []
    result_blocks = driver.find_elements("css selector", "div[data-rpos]")
    for block in result_blocks:
        try:
            link_el = block.find_element("css selector", "div.yuRUbf a")
            title_el = link_el.find_element("css selector", "h3")
            title = title_el.text
            link = link_el.get_attribute("href")
        except Exception:
            continue  # some blocks are ads/knowledge panels, not organic results

        snippet = ""
        try:
            snippet_el = block.find_element("css selector", "div.VwiC3b")
            snippet = snippet_el.text
        except Exception:
            pass

        if link and "linkedin.com" in link:
            results.append({"title": title, "link": link, "snippet": snippet})

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
        return set(data.get("urls", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_snapshot(query_label: str, urls: set[str]):
    path = snapshot_path(query_label)
    path.write_text(json.dumps({"urls": sorted(urls)}, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Search LinkedIn (via direct Google search, Selenium) for YC batch mentions"
    )
    parser.add_argument("--batch-tag", default="YC F26")
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run with a visible browser window instead of headless (lower detection risk, but needs a display)",
    )
    args = parser.parse_args()
    batch_tag = args.batch_tag

    try:
        driver = make_driver(headless=not args.headful)
    except Exception as e:
        print(f"ERROR: could not launch Chrome: {e}", file=sys.stderr)
        print(
            "Make sure a real Chrome/Chromium browser is installed on this "
            "machine (undetected-chromedriver drives an existing install, "
            "it doesn't bundle its own browser).",
            file=sys.stderr,
        )
        sys.exit(1)

    any_new = False
    try:
        print("Warming up (visiting google.com first, pausing before searching)...")
        warm_up(driver)

        for label, template in QUERY_TEMPLATES:
            query = template.format(batch_tag=batch_tag)
            query_label = f"{label}_{batch_tag}"

            print(f"\n=== Query [{label}]: {query} ===")
            try:
                results = run_google_search(
                    driver, query, allow_manual_captcha_solve=args.headful
                )
            except Exception as e:
                print(f"ERROR: search failed for query '{query}': {e}", file=sys.stderr)
                continue

            if not results:
                print(
                    "  No results returned. If this happens on a query you "
                    "know has results, the CSS selectors likely need "
                    "updating; see the docstring in run_google_search()."
                )
                continue

            current_urls = {r["link"] for r in results}
            has_prior_snapshot = snapshot_path(query_label).exists()
            last_known = load_last_snapshot(query_label)
            new_urls = current_urls - last_known if has_prior_snapshot else set()

            for r in results:
                flag = "  [NEW]" if r["link"] in new_urls else ""
                print(f"  - {r['title']}{flag}")
                print(f"      {r['link']}")
                if r["snippet"]:
                    print(f"      \"{r['snippet']}\"")

            if has_prior_snapshot:
                if new_urls:
                    any_new = True
                    print(f"\n  {len(new_urls)} new result(s) since last run.")
                else:
                    print("\n  No new results since last run.")
            else:
                print(
                    "\n  (No prior snapshot for this query; treated as "
                    "baseline. Run again later to see new results flagged "
                    "as [NEW].)"
                )

            save_snapshot(query_label, current_urls)

            # Space out the two queries within a run: back-to-back
            # automated searches are a stronger signal than a human would
            # produce, who'd naturally pause between searches. Widened
            # further after the first live run triggered a CAPTCHA
            # immediately on a cold session.
            human_like_delay(6, 14)

        if any_new:
            print("\n>>> At least one query surfaced new results this run. <<<")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
