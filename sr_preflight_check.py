"""
sr_preflight_check.py

Run this before turning automatic mode on (or any time you're not sure
things are still working): checks the things that silently break the
pipeline without it necessarily erroring loudly:
  - Required credentials are actually set (ANTHROPIC_API_KEY,
    SMTP_USERNAME, SMTP_PASSWORD)
  - The SMTP login actually works (a real login attempt, no email sent)
  - The LinkedIn Chrome profile exists (does NOT confirm the login
    session is still valid; LinkedIn sessions can expire; only that
    the profile directory itself is there; a real login check would mean
    launching a visible browser, which this intentionally avoids so the
    check stays fast and can run unattended)
  - Chrome/Chromium is actually installed and its version is detectable

Does NOT call the Anthropic API (that costs money); only checks the key
is present and shaped like a real key.

Usage:
    python sr_preflight_check.py
"""

import os
import smtplib
import ssl
import sys
from pathlib import Path

PROFILE_DIR = Path("li_chrome_profile").resolve()

RESULTS = []


def check(label: str, ok: bool, detail: str = ""):
    RESULTS.append((label, ok, detail))
    status = "OK  " if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f": {detail}"
    print(line)
    return ok


def check_env_vars():
    for name in ("ANTHROPIC_API_KEY", "SMTP_USERNAME", "SMTP_PASSWORD"):
        value = os.environ.get(name)
        if not value:
            check(f"{name} set", False, "not set; see README.md")
            continue
        if name == "ANTHROPIC_API_KEY" and not value.startswith("sk-ant-"):
            check(f"{name} set", False, "set, but doesn't look like a real Anthropic key (expected sk-ant-...)")
            continue
        check(f"{name} set", True, f"{len(value)} chars")


def check_smtp_login():
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    if not username or not password:
        check("SMTP login", False, "skipped; SMTP_USERNAME/SMTP_PASSWORD not set")
        return
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=10) as s:
            s.login(username, password)
        check("SMTP login", True, f"logged in as {username}")
    except Exception as e:
        check("SMTP login", False, str(e))


def check_chrome_profile():
    if not PROFILE_DIR.exists():
        check(
            "LinkedIn Chrome profile exists", False,
            f"not found at {PROFILE_DIR}; run: python li_native_search.py --login-only",
        )
        return
    check("LinkedIn Chrome profile exists", True, str(PROFILE_DIR))
    print(
        "         Note: this only confirms the profile directory exists, "
        "not that the LinkedIn login inside it is still valid; sessions "
        "expire. If sr_li_native_search.py comes back empty, re-run "
        "`python li_native_search.py --login-only`."
    )


def check_chrome_detectable():
    try:
        import undetected_chromedriver as uc
    except ImportError:
        check("Chrome detectable", False, "undetected_chromedriver not installed; pip install -r requirements.txt")
        return
    exe = uc.find_chrome_executable()
    if not exe:
        check("Chrome detectable", False, "no Chrome/Chromium install found")
        return
    from sr_google_search import detect_chrome_major_version
    version = detect_chrome_major_version()
    check("Chrome detectable", version is not None, f"{exe} (version {version})")


def main():
    print("Running SR007 pipeline preflight check...\n")
    check_env_vars()
    check_smtp_login()
    check_chrome_profile()
    check_chrome_detectable()

    print()
    failed = [label for label, ok, _ in RESULTS if not ok]
    if failed:
        print(f"{len(failed)} check(s) failed: {', '.join(failed)}")
        print("Fix these before turning on automatic mode.")
        sys.exit(1)
    print("All checks passed. Safe to run manually or turn on automatic mode.")


if __name__ == "__main__":
    main()
