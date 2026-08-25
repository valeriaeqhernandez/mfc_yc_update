"""
yc_configure.py

Mirrors sr_configure.py for the YC pipeline: the terminal menu for
day-to-day use: turn automatic mode on/off, change how often it runs,
change who gets emailed, change the tracked batch, trigger a manual run,
or run a sanity check. Everything it changes is saved to yc_config.json.

Uses a SEPARATE launchd job from the Speedrun pipeline
(capital.multifaceted.yc-pipeline vs capital.multifaceted.sr-pipeline) so
each pipeline's automatic mode can be turned on/off independently.

IMPORTANT: automatic mode runs with --non-interactive, meaning if Google
shows a CAPTCHA or LinkedIn shows a checkpoint page during a scheduled
run, it gets skipped rather than waited-on (nobody's there to solve it);
see li_unified_leads.py / li_native_search.py. If runs start coming back
oddly empty, run `python yc_preflight_check.py` and consider re-running
`python li_native_search.py --login-only`.

Usage:
    python yc_configure.py
"""

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from yc_config import load_config, save_config

PLIST_LABEL = "capital.multifaceted.yc-pipeline"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "yc-pipeline.log"
REPO_DIR = Path(__file__).resolve().parent
REQUIRED_ENV_VARS = ("ANTHROPIC_API_KEY", "SMTP_USERNAME", "SMTP_PASSWORD")


def launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def write_plist(interval_hours: float):
    env_vars = {name: os.environ.get(name, "") for name in REQUIRED_ENV_VARS}
    # See the matching comment in sr_configure.py's write_plist(); without
    # this, print() output under launchd sits in a buffer and never
    # reaches the log file until the process exits cleanly.
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
        <string>{escape(str(REPO_DIR / "yc_run_pipeline.py"))}</string>
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
    """See the matching function in sr_configure.py."""
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
        f"\nYC pre-MFN pipeline: current settings\n"
        f"  Batch tag:       {config['batch_tag']}\n"
        f"  YC batch slug:   {config['yc_batch_slug']}\n"
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
        "  7) Change batch (batch tag + YC batch slug)\n"
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
            # No plist reload needed: send_leads_report.py reads
            # yc_config.json fresh on every run, recipients aren't baked in.

        elif choice == "5":
            import yc_run_pipeline
            yc_run_pipeline.run(interactive=True, recipients=config["recipients"])

        elif choice == "6":
            subprocess.run([sys.executable, str(REPO_DIR / "yc_preflight_check.py")])

        elif choice == "7":
            new_tag = input(f"New batch tag, e.g. \"YC W27\" (current: {config['batch_tag']}): ").strip()
            new_slug = input(f"New YC batch slug, e.g. winter-2027 (current: {config['yc_batch_slug']}): ").strip()
            if not new_tag or not new_slug:
                print("Need both a batch tag and a batch slug.\n")
                continue
            print(
                f"\nSwitching to {new_tag} / {new_slug} doesn't touch "
                f"{config['batch_tag']}'s files; leads_{config['yc_batch_slug']}.json "
                "and its snapshot/report files are kept as-is; the new batch "
                "gets its own leads_<new-slug>.json.\n"
            )
            confirm = input(f"Confirm switch to {new_tag} / {new_slug}? [y/N] ").strip().lower()
            if confirm != "y":
                print("Not changed.\n")
                continue
            config["batch_tag"] = new_tag
            config["yc_batch_slug"] = new_slug
            save_config(config)
            print(f"Batch set to {new_tag} / {new_slug}.\n")

        elif choice in ("8", "q", "quit", "exit"):
            break

        else:
            print("Not a valid choice.\n")


if __name__ == "__main__":
    main()
