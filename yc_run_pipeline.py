"""
yc_run_pipeline.py

Runs the full YC pre-MFN sourcing pipeline end to end, in the right
order, with one command; mirrors sr_run_pipeline.py. Unlike the
Speedrun pipeline, this is only 3 steps: li_unified_leads.py already
internally covers the YC-directory fetch, Google search, and both
LinkedIn searches in one script.

    python li_unified_leads.py --batch-tag ... --yc-batch-slug ...
    python classify_leads.py --input leads_<slug>.json
    python send_leads_report.py --input leads_<slug>.json

This is what both manual runs and automatic mode (see yc_configure.py)
actually invoke.

Requires ANTHROPIC_API_KEY, SMTP_USERNAME, SMTP_PASSWORD to already be set
as environment variables (see README.md) and a logged-in LinkedIn session
in li_chrome_profile/ (run `python li_native_search.py --login-only` once
if you haven't). Run `python yc_preflight_check.py` first if unsure.

Usage:
    python yc_run_pipeline.py                  # normal manual run
    python yc_run_pipeline.py --non-interactive # for scheduled/automatic runs:
                                                   skips CAPTCHAs/checkpoints
                                                   instead of pausing for a
                                                   human who isn't there
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import classify_leads
import li_unified_leads
import send_leads_report
from yc_config import load_config

LOCK_PATH = Path(".yc_run_pipeline.lock")


def step(label: str):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal; treat as alive to be safe
    return True


def _acquire_lock() -> bool:
    """See the matching function in sr_run_pipeline.py; prevents two
    overlapping runs from launching concurrent Chrome sessions against
    the same shared LinkedIn profile."""
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text().strip())
        except ValueError:
            pid = None
        if pid is not None and _pid_alive(pid):
            return False
    LOCK_PATH.write_text(str(os.getpid()))
    return True


def _release_lock():
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def run(interactive: bool = True, recipients: list[str] | None = None):
    if not _acquire_lock():
        print(
            "Another yc_run_pipeline.py is already running (lock file "
            f"present at {LOCK_PATH}); skipping this run rather than "
            "starting a second, overlapping one against the same shared "
            "LinkedIn profile. If you're sure nothing is actually running "
            f"(e.g. after a crash), delete {LOCK_PATH} and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        started = datetime.now(timezone.utc)
        config = load_config()
        recipients = recipients or config["recipients"]

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY is not set. See README.md.", file=sys.stderr)
            sys.exit(1)
        for env_var in ("SMTP_USERNAME", "SMTP_PASSWORD"):
            if not os.environ.get(env_var):
                print(f"ERROR: {env_var} is not set. See README.md.", file=sys.stderr)
                sys.exit(1)

        step("1/3: Unified lead sourcing (YC directory + Google + LinkedIn)")
        li_unified_leads.run(config["batch_tag"], config["yc_batch_slug"], interactive=interactive)
        leads_path = Path(f"leads_{config['yc_batch_slug']}.json")

        step("2/3: AI classification")
        classify_leads.run(leads_path, api_key, classify_leads.DEFAULT_MODEL)

        step("3/3: Build report + email")
        send_leads_report.run(leads_path, recipients)

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"\nDone in {elapsed / 60:.1f} minutes.")
    finally:
        _release_lock()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the full YC pre-MFN sourcing pipeline")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="For scheduled/automatic runs: skip Google CAPTCHAs / LinkedIn checkpoints instead of pausing for manual solve",
    )
    parser.add_argument(
        "--recipients",
        nargs="+",
        default=None,
        help="Override the configured recipient list for this run only",
    )
    args = parser.parse_args()
    run(interactive=not args.non_interactive, recipients=args.recipients)
