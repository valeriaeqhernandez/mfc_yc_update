"""
classify_sr_leads.py: adapted from classify_leads.py

Batched Anthropic API call to separate genuine SR007-membership signals
from noise that survived regex extraction (e.g. an alumni account casually
mentioning "SR007" in a "congrats to the new cohort" post about SOMEONE
ELSE's company, not their own).

Same batching approach as the YC classifier: send N roster entries per call
with their evidence snippets, ask for structured JSON back, never free text.
Uses the same requests-based call (not the anthropic SDK, which isn't a
dependency anywhere else in this project) and the same ANTHROPIC_API_KEY
env var as classify_leads.py.

Roster entries carrying a "manual_verdict" (pre-seeded, human-confirmed
entries) skip the API call entirely and pass straight through; no point
spending a classification call re-deciding something already known.

Setup:
    pip install requests
    export ANTHROPIC_API_KEY=your_key_here

Usage:
    python classify_sr_leads.py
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

from anthropic_cost import UsageTracker
from sr_queries import CURRENT_BATCH

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"

# Namespaced by batch: see the comment on ROSTER_PATH in sr_unified_leads.py.
ROSTER_PATH = Path(f"sr_roster_{CURRENT_BATCH}.json")
CLASSIFIED_PATH = Path(f"sr_roster_classified_{CURRENT_BATCH}.json")
BATCH_SIZE = 15

SYSTEM_PROMPT = """You are screening candidate company names for whether they are \
genuinely part of a16z Speedrun's SR007 cohort, based on public evidence snippets \
(LinkedIn posts, LinkedIn headlines, Google search snippets).

Mark a candidate CONFIRMED only if the evidence clearly shows:
- A founder/team member of THAT company stating THEY joined/were selected for/are \
part of SR007, OR
- A16z Speedrun staff naming that company as a current SR007 portfolio company \
(e.g. in a hiring post, event post, or funding-announcement post)

Mark REJECTED if the evidence is:
- An applications-open/closed announcement with no company-specific claim
- A scout/referral/"DM me to apply" post
- A mention of a DIFFERENT batch (SR001-SR006, or a future SR008)
- Too vague to tell (e.g. batch keyword present but no clear company/founder claim)
- Not actually a company name (a person's name misfired as a company, a generic noun)

Mark UNCERTAIN if there's a real signal but it's ambiguous or single-sourced with weak \
wording: worth a human second look, not worth confidently reporting.

Respond with ONLY a JSON array, one object per candidate in the same order given, no \
other text:
[{"normalized_name": "...", "verdict": "CONFIRMED|REJECTED|UNCERTAIN", "reason": "one sentence"}, ...]"""


def batched(items, n):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def build_user_message(entries: list[tuple[str, dict]]) -> str:
    payload = [
        {
            "normalized_name": key,
            "display_name": v["display_name"],
            "evidence_snippets": [e["snippet"] for e in v["evidence"][:3]],
        }
        for key, v in entries
    ]
    return json.dumps(payload, indent=2)


def classify_batch(entries: list[tuple[str, dict]], api_key: str, model: str, tracker: UsageTracker) -> list[dict]:
    resp = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            # Confirmed live: 2000 was too tight for a 15-entry batch and
            # truncated mid-JSON-string, throwing away that whole batch's
            # verdicts (including several real CONFIRMED companies) to the
            # generic "classification call failed" fallback. This is a cap,
            # not a spend target -- raising it costs nothing unless the
            # model actually needs the room.
            "max_tokens": 4000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_user_message(entries)}],
        },
        timeout=120,  # was 60s; a real batch call timed out at that limit as the roster grew
    )
    resp.raise_for_status()
    data = resp.json()
    tracker.add(data.get("usage", {}))

    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    raw_text = "".join(text_blocks).strip()

    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Could not parse Claude's response as JSON. Raw response:\n{raw_text}"
        ) from e

    if not isinstance(parsed, list) or len(parsed) != len(entries):
        raise RuntimeError(
            f"Expected a JSON array of {len(entries)} items, got: {parsed!r}"
        )

    return parsed


def run(api_key: str, model: str):
    started = time.monotonic()
    roster = json.loads(ROSTER_PATH.read_text())
    items = list(roster.items())

    verdicts = {}
    tracker = UsageTracker(model)

    # Pre-seeded, manually-verified entries skip the API entirely.
    manual = [(k, v) for k, v in items if v.get("manual_verdict")]
    for key, entry in manual:
        verdicts[key] = {
            "normalized_name": key,
            "verdict": entry["manual_verdict"],
            "reason": entry.get("manual_verdict_reason", "manually verified"),
        }
    if manual:
        print(f"{len(manual)} entr(y/ies) pre-verified: skipping API classification for these.")

    to_classify = [(k, v) for k, v in items if not v.get("manual_verdict")]
    print(f"Classifying {len(to_classify)} entries via {model}...")

    for batch in batched(to_classify, BATCH_SIZE):
        print(f"  Batch of {len(batch)}...")
        try:
            results = classify_batch(batch, api_key, model, tracker)
        except Exception as e:
            print(f"  ERROR classifying this batch: {e}", file=sys.stderr)
            for key, entry in batch:
                verdicts[key] = {
                    "normalized_name": key,
                    "verdict": "UNCERTAIN",
                    "reason": f"classification call failed: {e}",
                }
            continue
        for v in results:
            verdicts[v["normalized_name"]] = v

    classified = {}
    for key, entry in roster.items():
        v = verdicts.get(key, {"verdict": "UNCERTAIN", "reason": "not classified"})
        classified[key] = {**entry, "verdict": v["verdict"], "verdict_reason": v["reason"]}

    CLASSIFIED_PATH.write_text(json.dumps(classified, indent=2))
    counts = {}
    for v in classified.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    print(f"\nClassified {len(classified)} entries: {counts}")
    print(f"Wrote {CLASSIFIED_PATH}")
    elapsed = time.monotonic() - started
    print(f"Classification step: {tracker.summary()}, took {elapsed:.1f}s.")
    return classified


def main():
    parser = argparse.ArgumentParser(description="AI relevance/verdict classifier for SR007 roster")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: set ANTHROPIC_API_KEY as an environment variable.", file=sys.stderr)
        sys.exit(1)

    if not ROSTER_PATH.exists():
        print(f"ERROR: {ROSTER_PATH} not found. Run sr_unified_leads.py first.", file=sys.stderr)
        sys.exit(1)

    run(api_key, args.model)


if __name__ == "__main__":
    main()
