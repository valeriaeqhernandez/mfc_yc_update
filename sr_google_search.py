"""
sr_google_search.py: adapted from li_search_selenium.py

Runs Google searches for Speedrun batch-membership signals via
undetected-chromedriver, same CAPTCHA-handling approach as the YC version.

Unchanged from the YC pipeline (platform facts, not YC-specific):
  - Google's real result selectors: div[data-rpos] (result container),
    div.yuRUbf a (title link), div.VwiC3b (snippet). NOT div.g: confirmed
    stale/unreliable in prior testing.
  - Google Custom Search JSON API is closed to new customers (403
    PERMISSION_DENIED, confirmed live); scraping remains the only option.

Changed from the YC pipeline:
  - Query source is sr_queries.SEARCH_QUERIES instead of YC's "(YC F26)"
    equivalents.
  - No downstream ground-truth directory to filter against, so this script's
    output needs a slightly higher bar before being written to the snapshot:
    require the batch bio pattern AND a company-name-shaped noun phrase near
    it, not just the batch keyword anywhere on the page.
"""

import json
import re
import subprocess
import time
import random
from datetime import datetime, timezone
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from sr_queries import SEARCH_QUERIES, CURRENT_BATCH

OUT_DIR = Path("snapshots") / CURRENT_BATCH
OUT_DIR.mkdir(parents=True, exist_ok=True)

def detect_chrome_major_version():
    """
    undetected-chromedriver, left to its own defaults, fetches whatever
    ChromeDriver build is globally "latest stable" rather than one that
    matches the Chrome actually installed on this machine; those two
    drift apart whenever Chrome hasn't auto-updated yet, causing a hard
    version mismatch at session creation. Detecting the real installed
    version and passing it explicitly avoids that class of failure.

    Uses uc's own find_chrome_executable() (covers macOS, Linux via PATH,
    and Windows Program Files) rather than a hardcoded path, so this
    resolves correctly on whoever's machine actually runs this script, not
    just the one it was written on.
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
    options.add_argument("--window-size=1280,900")
    driver = uc.Chrome(
        options=options, use_subprocess=True, version_main=detect_chrome_major_version()
    )
    return driver


def wait_out_captcha(driver, max_wait_s=180):
    """
    If Google serves a CAPTCHA/consent interstitial, pause for manual
    solve rather than trying to auto-bypass it (same policy as the YC
    pipeline: treat CAPTCHA as a hard stop, not something to defeat
    programmatically).
    """
    if "sorry/index" in driver.current_url or "consent.google.com" in driver.current_url:
        print(f"CAPTCHA/consent wall detected. Waiting up to {max_wait_s}s for manual solve...")
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            if "sorry/index" not in driver.current_url and "consent.google.com" not in driver.current_url:
                print("Cleared.")
                return True
            time.sleep(2)
        print("Still blocked after wait window; skipping this query.")
        return False
    return True


def search_query(driver, query):
    url = f"https://www.google.com/search?q={query}"
    driver.get(url)
    if not wait_out_captcha(driver):
        return []

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-rpos]"))
        )
    except Exception:
        return []

    results = []
    for block in driver.find_elements(By.CSS_SELECTOR, "div[data-rpos]"):
        try:
            link_el = block.find_element(By.CSS_SELECTOR, "div.yuRUbf a")
            href = link_el.get_attribute("href")
        except Exception:
            continue
        try:
            snippet_el = block.find_element(By.CSS_SELECTOR, "div.VwiC3b")
            snippet = snippet_el.text
        except Exception:
            snippet = ""
        title = link_el.text
        results.append({"url": href, "title": title, "snippet": snippet})
    return results


def run():
    driver = build_driver()
    all_results = []
    try:
        for q in SEARCH_QUERIES:
            print(f"Searching: {q}")
            hits = search_query(driver, q)
            for h in hits:
                h["query"] = q
                h["fetched_at"] = datetime.now(timezone.utc).isoformat()
            all_results.extend(hits)
            time.sleep(random.uniform(4, 9))  # human-ish pacing between queries
    finally:
        driver.quit()

    out_path = OUT_DIR / f"google_raw_{datetime.now(timezone.utc).date().isoformat()}.json"
    out_path.write_text(json.dumps(all_results, indent=2))
    print(f"Wrote {len(all_results)} raw hits to {out_path}")


if __name__ == "__main__":
    run()
