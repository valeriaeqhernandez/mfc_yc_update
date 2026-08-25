"""
classify_leads.py

Adds an AI relevance pass on top of li_unified_leads.py's output. Regex/
keyword matching structurally can't tell "Rachel Huang joined YC F26" (a
real signal) apart from "Max Kolysh: the YC F26 deadline is in 7 days,
DM me if you're on the fence" (real text, mentions the batch tag, but
isn't a join signal at all); that distinction needs actual reading
comprehension, which is what this adds.

Sends each lead's snippet to Claude, asking a single question: does this
text indicate that a SPECIFIC PERSON OR COMPANY has joined/been accepted
into this YC batch; as opposed to generic YC content (deadline
reminders, "should I apply" encouragement, alumni-of-a-different-batch
mentions, unrelated hashtag posts).

Designed to be source-agnostic on purpose: it only looks at the text of
each lead, not which pipeline found it; so the same function will work
unmodified on a future Twitter/X source, once one exists. Adding that
source later means writing a normalized-lead-producing function (same
shape as the others in li_unified_leads.py) and running it through this
same classifier: no changes needed here.

Setup:
    pip install requests
    export ANTHROPIC_API_KEY=your_key_here

Usage:
    python classify_leads.py --input leads_fall-2026.json
    (writes the result back into the same file, adding "ai_relevant" and
    "ai_reason" fields to each lead)
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are screening leads for a venture capital firm sourcing newly-accepted \
Y Combinator startups before they're publicly listed. Each lead is a snippet of text \
(a LinkedIn post, headline, or search result) that mentions a specific YC batch tag.

Your job: decide whether the text is a genuine SIGNAL that a specific person or company \
has joined/been accepted into that batch; as opposed to generic content that merely \
mentions the batch tag without being a join signal. Examples of NOT a signal:
- Application deadline reminders ("the deadline is in 7 days")
- Generic encouragement to apply ("if you're on the fence, DM me")
- A YC partner or alum talking about the batch in general, not their own company
- Hashtag spam or unrelated posts that happen to contain the batch tag
- Mentions of a DIFFERENT batch than the one being searched for

Examples of a genuine signal:
- "I got into YC F26!"
- "Co-Founder @ Collar (YC F26)" as a headline/bio tag
- "Simantic is joining Y Combinator for the Fall 2026 batch"
- A company's own LinkedIn page announcing they joined YC

Respond with ONLY a JSON array, one object per lead in the same order given, no other text:
[{"relevant": true/false, "reason": "one short phrase"}, ...]"""


def load_leads(path: Path) -> dict:
    return json.loads(path.read_text())


def build_user_message(leads: list[dict]) -> str:
    lines = ["Classify these leads:\n"]
    for i, lead in enumerate(leads):
        snippet = lead.get("snippet", "")[:400]
        lines.append(f'{i}. Name: "{lead.get("name") or "?"}" | Text: "{snippet}"')
    return "\n".join(lines)


def call_claude(leads: list[dict], api_key: str, model: str) -> list[dict]:
    """
    Sends all leads in ONE request (cheaper, fewer calls, and gives the
    model useful context across leads) rather than one call per lead.
    Returns a list of {"relevant": bool, "reason": str} in the same order
    as the input leads.
    """
    resp = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_user_message(leads)}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    raw_text = "".join(text_blocks).strip()

    # Defensive parsing: strip markdown code fences if the model added them
    # despite being asked not to, and fail loudly rather than silently on
    # anything else, since a silent parse failure here would mean every
    # lead gets a wrong or missing relevance judgment.
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Could not parse Claude's response as JSON. Raw response:\n{raw_text}"
        ) from e

    if not isinstance(parsed, list) or len(parsed) != len(leads):
        raise RuntimeError(
            f"Expected a JSON array of {len(leads)} items, got: {parsed!r}"
        )

    return parsed


def run(input_path: Path, api_key: str, model: str, batch_size: int = 25):
    """
    Factored out of main() so yc_run_pipeline.py can call this directly
    instead of shelling out.
    """
    if not input_path.exists():
        print(f"ERROR: {input_path} not found.", file=sys.stderr)
        sys.exit(1)

    data = load_leads(input_path)
    leads = data.get("leads", [])
    print(f"Classifying {len(leads)} leads via {model}...")

    for start in range(0, len(leads), batch_size):
        batch = leads[start : start + batch_size]
        print(f"  Batch {start}-{start + len(batch)}...")
        try:
            results = call_claude(batch, api_key, model)
        except Exception as e:
            print(f"  ERROR classifying this batch: {e}", file=sys.stderr)
            print("  Leaving these leads unclassified (no ai_relevant field added).", file=sys.stderr)
            continue

        for lead, result in zip(batch, results):
            lead["ai_relevant"] = result.get("relevant")
            lead["ai_reason"] = result.get("reason")

    flagged = [l for l in leads if l.get("ai_relevant") is False]
    print(f"\n{len(flagged)} lead(s) flagged as likely NOT a genuine signal:\n")
    for l in flagged:
        print(f"  - {l.get('name') or '(name unknown)'}: {l.get('ai_reason')}")
        print(f"      {l.get('link', '')}")

    input_path.write_text(json.dumps(data, indent=2))
    print(f"\nUpdated {input_path} with ai_relevant / ai_reason fields.")


def main():
    parser = argparse.ArgumentParser(description="AI relevance filter for sourced leads")
    parser.add_argument("--input", default="leads_fall-2026.json", help="Path to the leads JSON file")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="Leads per API call (default 25, keeps prompts a reasonable size)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: set ANTHROPIC_API_KEY as an environment variable.", file=sys.stderr)
        sys.exit(1)

    run(Path(args.input), api_key, args.model, args.batch_size)


if __name__ == "__main__":
    main()
