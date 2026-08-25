"""
sr_configure.py

The one command meant for day-to-day use: a terminal menu to turn
automatic mode on/off, change how often it runs, change who gets emailed,
trigger a manual run, or run a sanity check. Everything it changes is
saved to sr_config.json.

"Automatic mode" is implemented as a macOS launchd agent (not a background
Python loop); it survives reboots and doesn't die when you close the
terminal, and is the standard way to schedule recurring jobs on macOS.
Turning it on writes a small job file to
~/Library/LaunchAgents/capital.multifaceted.sr-pipeline.plist and loads
it; turning it off unloads it. You never need to look at that file.

IMPORTANT: automatic mode runs with --non-interactive, meaning if
LinkedIn shows a checkpoint/verification page during a scheduled run, it
gets skipped rather than waited-on (nobody's there to solve it); see
sr_li_native_search.py. If runs start coming back oddly empty, run
`python sr_preflight_check.py` and consider re-running
`python li_native_search.py --login-only`.

Usage:
    python sr_configure.py
"""

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from sr_config import load_config, save_config

PLIST_LABEL = "capital.multifaceted.sr-pipeline"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "sr-pipeline.log"
REPO_DIR = Path(__file__).resolve().parent
REQUIRED_ENV_VARS = ("ANTHROPIC_API_KEY", "SMTP_USERNAME", "SMTP_PASSWORD")


def launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def write_plist(interval_hours: float):
    env_vars = {name: os.environ.get(name, "") for name in REQUIRED_ENV_VARS}
    # Without this, Python fully buffers stdout when it's not a live
    # terminal (i.e. always, under launchd); print() output sits in
    # memory and never reaches the log file until the process exits
    # cleanly, so a still-running or killed-mid-run job shows nothing in
    # the log even though it did real work. Confirmed live: a test run
    # produced only a stray cleanup warning in the log, no actual
    # progress output, because it never got the chance to exit normally.
    env_vars["PYTHONUNBUFFERED"] = "1"
    env_xml = "\n".join(
        f"        <key>{name}</key><string>{escape(value)}</string>"
        for name, value in env_vars.items()
    )
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{escape(sys.executable)}</string>
        <string>{escape(str(REPO_DIR / "sr_run_pipeline.py"))}</string>
        <string>--non-interactive</string>
    </array>
    <key>WorkingDirectory</key><string>{escape(str(REPO_DIR))}</string>
    <key>StartInterval</key><integer>{int(interval_hours * 3600)}</integer>
    <key>StandardOutPath</key><string>{escape(str(LOG_PATH))}</string>
    <key>StandardErrorPath</key><string>{escape(str(LOG_PATH))}</string>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
</dict>
</plist>
"""
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist)
    PLIST_PATH.chmod(0o600)  # contains the raw API key + SMTP password


def is_job_running() -> bool:
    """
    True if launchd currently has a running instance of this job (a real
    PID, not just "loaded but idle"). Used to warn before a reload that
    would kill it mid-run; confirmed live that unloading an in-progress
    scheduled run (e.g. from changing the interval while automatic mode
    is already on) silently kills it partway through, with no output
    reaching the log since it never exits cleanly.
    """
    result = launchctl("list", PLIST_LABEL)
    if result.returncode != 0:
        return False  # not loaded at all
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('"PID"'):
            return True
    return False


def enable_automatic(interval_hours: float) -> bool:
    missing = [n for n in REQUIRED_ENV_VARS if not os.environ.get(n)]
    if missing:
        print(
            f"Can't enable automatic mode: {', '.join(missing)} not set in "
            "this terminal. Set them (see README.md), open a fresh terminal, "
            "and try again; launchd bakes in whatever's set right now."
        )
        return False

    if PLIST_PATH.exists() and is_job_running():
        print(
            "NOTE: a scheduled run looks like it's actively in progress right "
            "now; reloading will interrupt it partway through (unloading a "
            "launchd job kills its running instance). It'll just run again "
            "at the next scheduled interval, so nothing's broken, but you "
            "won't see this run's results."
        )

    write_plist(interval_hours)
    launchctl("unload", str(PLIST_PATH))  # fine if it wasn't loaded yet
    result = launchctl("load", "-w", str(PLIST_PATH))
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr.strip()}")
        return False
    print(f"Automatic mode ON: runs every {interval_hours:g} hour(s). Logs: {LOG_PATH}")
    return True


def disable_automatic():
    if PLIST_PATH.exists():
        launchctl("unload", "-w", str(PLIST_PATH))
    print("Automatic mode OFF.")


def print_status(config: dict):
    mode = "ON" if config["automatic_mode"] else "OFF"
    print(
        f"\na16z Speedrun pipeline: current settings\n"
        f"  Batch:           {config['batch']}\n"
        f"  Automatic mode:  {mode}\n"
        f"  Run interval:    every {config['run_interval_hours']:g} hour(s)\n"
        f"  Recipients:      {', '.join(config['recipients'])}\n"
    )


def prompt_menu() -> str:
    return input(
        "What do you want to do?\n"
        "  1) Turn automatic mode ON\n"
        "  2) Turn automatic mode OFF\n"
        "  3) Change run interval\n"
        "  4) Change recipients\n"
        "  5) Run the pipeline right now (manual run)\n"
        "  6) Run a preflight check\n"
        "  7) Change batch (e.g. SR007 -> SR008)\n"
        "  8) Quit\n"
        "> "
    ).strip()


def main():
    config = load_config()
    while True:
        print_status(config)
        choice = prompt_menu()

        if choice == "1":
            if enable_automatic(config["run_interval_hours"]):
                config["automatic_mode"] = True
                save_config(config)

        elif choice == "2":
            disable_automatic()
            config["automatic_mode"] = False
            save_config(config)

        elif choice == "3":
            raw = input(f"New interval in hours (current {config['run_interval_hours']:g}): ").strip()
            try:
                hours = float(raw)
                if hours <= 0:
                    raise ValueError
            except ValueError:
                print("Enter a positive number of hours.\n")
                continue
            config["run_interval_hours"] = hours
            save_config(config)
            if config["automatic_mode"]:
                enable_automatic(hours)  # reload with the new interval

        elif choice == "4":
            raw = input(f"New recipients, comma-separated (current: {', '.join(config['recipients'])}): ").strip()
            recipients = [r.strip() for r in raw.split(",") if r.strip()]
            if not recipients:
                print("Need at least one recipient.\n")
                continue
            config["recipients"] = recipients
            save_config(config)
            # No plist reload needed: send_sr_leads_report.py reads
            # sr_config.json fresh on every run, recipients aren't baked in.

        elif choice == "5":
            import sr_run_pipeline
            sr_run_pipeline.run(interactive=True, recipients=config["recipients"])

        elif choice == "6":
            subprocess.run([sys.executable, str(REPO_DIR / "sr_preflight_check.py")])

        elif choice == "7":
            raw = input(f"New batch code, e.g. SR008 (current: {config['batch']}): ").strip().upper()
            if not raw:
                print("Enter a batch code.\n")
                continue
            print(
                f"\nSwitching to {raw} starts a completely separate roster, "
                f"classified list, and last-sent tracker for {raw}; "
                f"{config['batch']}'s data (sr_roster_{config['batch']}.json etc.) "
                "is kept, not touched, and you can switch back later without "
                "losing anything.\n"
                "You'll also want to update sr_queries.py's search patterns "
                "if the new batch's bio convention differs (e.g. a new "
                "sub-brand or number format); this only changes which files "
                "the pipeline reads/writes, not the query wording itself."
            )
            confirm = input(f"Confirm switch to {raw}? [y/N] ").strip().lower()
            if confirm != "y":
                print("Not changed.\n")
                continue
            config["batch"] = raw
            save_config(config)
            print(
                f"Batch set to {raw}. sr_run_pipeline.py reads sr_config.json "
                "fresh on every run (including scheduled automatic ones), so "
                "no plist reload is needed; just restart sr_configure.py "
                "itself if you're keeping this menu open.\n"
            )

        elif choice in ("8", "q", "quit", "exit"):
            break

        else:
            print("Not a valid choice.\n")


if __name__ == "__main__":
    main()
