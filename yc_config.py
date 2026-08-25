"""
yc_config.py

Mirrors sr_config.py for the YC pipeline: the one file meant to be
tweaked without touching code: which batch to track, who gets the report,
and whether/how often it runs automatically. Edit via yc_configure.py, not
by hand (a missing comma in yc_config.json breaks every script that reads
it).
"""

import json
from pathlib import Path

CONFIG_PATH = Path("yc_config.json")

DEFAULTS = {
    "batch_tag": "YC F26",
    "yc_batch_slug": "fall-2026",
    "recipients": ["querovaleria04@gmail.com"],
    "automatic_mode": False,
    "run_interval_hours": 12,
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULTS)
        return dict(DEFAULTS)
    data = json.loads(CONFIG_PATH.read_text())
    return {**DEFAULTS, **data}


def save_config(config: dict):
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
