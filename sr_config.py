"""
sr_config.py

The one file meant to be tweaked without touching any pipeline code:
who gets the report, how often it should run automatically, and whether
automatic mode is even on. Every script that needs these values reads them
from here instead of hardcoding them.

Don't hand-edit sr_config.json unless you're comfortable with JSON syntax
(a missing comma will break every script that reads it); run
`python sr_configure.py` instead, which edits it safely through a menu.
"""

import json
from pathlib import Path

CONFIG_PATH = Path("sr_config.json")

DEFAULTS = {
    "recipients": ["querovaleria04@gmail.com"],
    "automatic_mode": False,
    "run_interval_hours": 12,
    "batch": "SR007",
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULTS)
        return dict(DEFAULTS)
    data = json.loads(CONFIG_PATH.read_text())
    return {**DEFAULTS, **data}


def save_config(config: dict):
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
