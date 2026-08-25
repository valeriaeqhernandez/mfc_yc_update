"""
sr_run_pipeline.py

Runs the full a16z Speedrun sourcing pipeline end to end, in the right
order, with one command; instead of remembering to chain:
    python sr_google_search.py
    python sr_li_native_search.py
    python sr_unified_leads.py
    python classify_sr_leads.py
    python send_sr_leads_report.py

This is what both manual runs (`python sr_run_pipeline.py`) and automatic
mode (see sr_configure.py) actually invoke.

Requires ANTHROPIC_API_KEY, SMTP_USERNAME, SMTP_PASSWORD to already be set
as environment variables (see README.md) and a logged-in LinkedIn session
in li_chrome_profile/ (run `python li_native_search.py --login-only` once
if you haven't). Run `python sr_preflight_check.py` first if unsure.

Usage:
    python sr_run_pipeline.py                  # normal manual run
    python sr_run_pipeline.py --non-interactive # for scheduled/automatic runs:
                                                   skips LinkedIn checkpoints
                                                   instead of pausing for a
                                                   human who isn't there
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import classify_sr_leads
import send_sr_leads_report
import sr_google_search
import sr_li_native_search
import sr_unified_leads
from sr_config import load_config

LOCK_PATH = Path(".sr_run_pipeline.lock")


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
    """
    Prevents two overlapping runs from launching concurrent Chrome
    sessions against the same shared LinkedIn profile; confirmed live
    that a run interval shorter than the pipeline's own duration lets a
    second scheduled run start while the first is still executing,
    crashing one of them with a mid-session Chrome disconnect. Self-heals
    from a stale lock (e.g. left behind by a killed/crashed previous run,
    which doesn't get a chance to run cleanup) by checking whether the
    PID it names is actually still alive rather than trusting the file's
    mere existence.
    """
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
            "Another sr_run_pipeline.py is already running (lock file "
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

        step("1/5: Google search")
        sr_google_search.run()

        step("2/5: LinkedIn native search (posts + people)")
        sr_li_native_search.run(interactive=interactive)

        step("3/5: Merge into provisional roster")
        sr_unified_leads.run()

        step("4/5: AI classification")
        classify_sr_leads.run(api_key, classify_sr_leads.DEFAULT_MODEL)

        step("5/5: Build report + email")
        send_sr_leads_report.run(recipients)

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"\nDone in {elapsed / 60:.1f} minutes.")
    finally:
        _release_lock()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the full SR007 sourcing pipeline")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="For scheduled/automatic runs: skip LinkedIn checkpoints instead of pausing for manual solve",
    )
    parser.add_argument(
        "--recipients",
        nargs="+",
        default=None,
        help="Override the configured recipient list for this run only",
    )
    args = parser.parse_args()
    run(interactive=not args.non_interactive, recipients=args.recipients)
