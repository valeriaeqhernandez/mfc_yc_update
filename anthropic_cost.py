"""
anthropic_cost.py

Shared token-usage/cost tracking for the two AI classification steps
(classify_leads.py, classify_sr_leads.py). Reports exact token counts
(always accurate, straight from the API response's own "usage" field)
alongside an estimated dollar cost (accurate only as long as
PRICING_PER_MTOK below matches Anthropic's current rates).

Pricing last confirmed 2026-08-26 from console.anthropic.com. claude-
sonnet-5 is on introductory pricing through 2026-08-31; after that the
real rate reverts to $3.00 / $15.00 per 1M input/output tokens and this
file's estimate will run low until updated. console.anthropic.com ->
Settings -> Usage is always the source of truth for actual billed spend;
treat this as a same-ballpark estimate, not a bill.
"""

# (input $ / 1M tokens, output $ / 1M tokens)
PRICING_PER_MTOK = {
    "claude-sonnet-5": (2.00, 10.00),  # introductory rate through 2026-08-31
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
DEFAULT_PRICING = (3.00, 15.00)  # fallback for an unrecognized model


class UsageTracker:
    def __init__(self, model: str):
        self.model = model
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, usage: dict):
        self.input_tokens += usage.get("input_tokens", 0) or 0
        self.output_tokens += usage.get("output_tokens", 0) or 0

    def estimated_cost(self) -> float:
        in_rate, out_rate = PRICING_PER_MTOK.get(self.model, DEFAULT_PRICING)
        return (self.input_tokens / 1_000_000) * in_rate + (self.output_tokens / 1_000_000) * out_rate

    def summary(self) -> str:
        total = self.input_tokens + self.output_tokens
        return (
            f"{total:,} tokens ({self.input_tokens:,} in / {self.output_tokens:,} out), "
            f"~${self.estimated_cost():.4f} at current pricing "
            "(console.anthropic.com -> Usage has the exact billed figure)"
        )
