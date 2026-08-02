"""
yc_checker.py

Checks the YC public company directory for a given batch, compares it
against the last known list stored in a Google Sheet, appends any new
companies as leads, and emails an alert for each new lead found.

Run manually:
    python yc_checker.py

Intended to be run on a schedule (e.g. every 12 hours) via GitHub Actions,
cron, or similar — see README.md for setup.
"""

import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Config — set these via environment variables (see .env.example / README)
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Fetch a required environment variable with a clear error if missing."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"See .env.example / README.md for setup."
        )
    return value


YC_BATCH_URL = os.environ.get(
    "YC_BATCH_URL", "https://www.ycombinator.com/companies?batch=Fall%202026"
)
YC_BATCH_LABEL = os.environ.get("YC_BATCH_LABEL", "Fall 2026")

GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
)

LEADS_TAB_NAME = os.environ.get("LEADS_TAB_NAME", "Leads")
SNAPSHOT_TAB_NAME = os.environ.get("SNAPSHOT_TAB_NAME", "YC_Directory_Snapshot")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
ALERT_TO_EMAIL = os.environ.get("ALERT_TO_EMAIL", "smaiyl@multifaceted.capital")
# Note: GOOGLE_SHEET_ID, SMTP_USERNAME, SMTP_PASSWORD are required but are
# read lazily inside main()/send_alert_email() via _require_env(), so that
# individual helper functions (e.g. the HTML/JSON parsing logic) can be
# imported and unit-tested without needing every credential set.

LEADS_HEADER = [
    "Company name",
    "Founder(s)",
    "Source",
    "Source link",
    "Source snippet",
    "Date first spotted",
    "Batch tag seen",
    "Confidence tier",
    "Status",
]

# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_sheet_client():
    creds = Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return gspread.authorize(creds)


def get_or_create_worksheet(spreadsheet, title, header=None, rows=100, cols=10):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
        if header:
            ws.append_row(header)
    return ws


# ---------------------------------------------------------------------------
# YC directory scraping
# ---------------------------------------------------------------------------


def fetch_yc_companies(batch_url: str) -> set[str]:
    """
    Fetch the YC public directory page for a batch and return a set of
    company names currently listed.

    NOTE: YC's directory page is a JS-rendered (Next.js) app. A plain
    `requests` GET may only return the initial HTML shell without the
    company list, depending on how YC serves the page at request time.

    Two fallback strategies if this stops working:
      1. Check whether the page embeds a JSON payload in a <script> tag
         (common Next.js pattern: id="__NEXT_DATA__") and parse that
         instead of the rendered HTML.
      2. Use a maintained community project like yc-oss/api
         (https://github.com/yc-oss/api), which already solves this
         scraping problem and may expose a simpler JSON endpoint.

    This function tries a plain HTML parse first and should be treated
    as a starting point to verify/adjust once you can test against the
    live page.
    """
    resp = requests.get(
        batch_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        },
        timeout=20,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strategy 1: look for a Next.js embedded JSON payload first, since
    # it's more robust than scraping rendered card markup.
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        import json

        try:
            data = json.loads(next_data.string)
            names = _extract_company_names_from_next_data(data)
            if names:
                return names
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # fall through to HTML strategy

    # Strategy 2: fall back to scraping visible company name elements.
    # NOTE: these CSS selectors are a best guess and WILL likely need
    # adjustment based on the actual rendered page structure — inspect
    # the page in a browser dev tools and update this selector.
    names = set()
    for el in soup.select("[class*='company'] a[href*='/companies/']"):
        text = el.get_text(strip=True)
        if text:
            names.add(text)

    return names


def _extract_company_names_from_next_data(data: dict) -> set[str]:
    """
    Best-effort walk of the __NEXT_DATA__ payload looking for company
    name fields. Structure is not guaranteed stable — adjust the path
    below once you've inspected a real payload.
    """
    names = set()

    def walk(node):
        if isinstance(node, dict):
            # Common pattern: objects with a "name" key alongside a
            # "batch" or "slug" key indicating this is a company record.
            if "name" in node and ("batch" in node or "slug" in node):
                names.add(node["name"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return names


# ---------------------------------------------------------------------------
# Snapshot diffing
# ---------------------------------------------------------------------------


def load_last_snapshot(snapshot_ws) -> set[str]:
    values = snapshot_ws.col_values(1)  # column A, one company name per row
    # Skip header row if present
    if values and values[0].strip().lower() == "company name":
        values = values[1:]
    return set(v.strip() for v in values if v.strip())


def save_snapshot(snapshot_ws, companies: set[str]):
    snapshot_ws.clear()
    snapshot_ws.append_row(["Company name"])
    if companies:
        snapshot_ws.append_rows([[c] for c in sorted(companies)])


# ---------------------------------------------------------------------------
# Email alert
# ---------------------------------------------------------------------------


def send_alert_email(new_companies: list[str], sheet_url: str):
    if not new_companies:
        return

    smtp_username = _require_env("SMTP_USERNAME")
    smtp_password = _require_env("SMTP_PASSWORD")

    subject = f"[MFC Sourcing] {len(new_companies)} new YC {YC_BATCH_LABEL} compan{'y' if len(new_companies) == 1 else 'ies'} spotted"

    lines = [
        f"New compan{'y' if len(new_companies) == 1 else 'ies'} found on the YC {YC_BATCH_LABEL} directory:",
        "",
    ]
    for name in new_companies:
        lines.append(f"  - {name}")
    lines.append("")
    lines.append(f"Full leads sheet: {sheet_url}")
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_username
    msg["To"] = ALERT_TO_EMAIL
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(smtp_username, smtp_password)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    google_sheet_id = _require_env("GOOGLE_SHEET_ID")

    client = get_sheet_client()
    spreadsheet = client.open_by_key(google_sheet_id)

    leads_ws = get_or_create_worksheet(spreadsheet, LEADS_TAB_NAME, header=LEADS_HEADER)
    snapshot_ws = get_or_create_worksheet(
        spreadsheet, SNAPSHOT_TAB_NAME, header=["Company name"]
    )

    print(f"Fetching YC directory: {YC_BATCH_URL}")
    current_companies = fetch_yc_companies(YC_BATCH_URL)
    print(f"Found {len(current_companies)} companies currently listed.")

    if not current_companies:
        print(
            "WARNING: fetched zero companies. This likely means the scraping "
            "selectors need adjustment (see docstring in fetch_yc_companies). "
            "Aborting without overwriting the snapshot, to avoid false 'new "
            "company' alerts on the next run."
        )
        return

    last_known = load_last_snapshot(snapshot_ws)
    new_companies = sorted(current_companies - last_known)

    if new_companies:
        print(f"New companies detected: {new_companies}")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = [
            [
                name,           # Company name
                "",             # Founder(s) — fill in manually after review
                "YC Directory", # Source
                YC_BATCH_URL,   # Source link
                "",             # Source snippet
                today,          # Date first spotted
                YC_BATCH_LABEL, # Batch tag seen
                "Directory-listed",  # Confidence tier
                "New",          # Status
            ]
            for name in new_companies
        ]
        leads_ws.append_rows(rows)

        send_alert_email(new_companies, spreadsheet.url)
        print("Alert email sent.")
    else:
        print("No new companies since last check.")

    save_snapshot(snapshot_ws, current_companies)
    print("Snapshot updated.")


if __name__ == "__main__":
    main()