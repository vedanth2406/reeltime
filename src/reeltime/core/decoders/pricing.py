"""Model pricing, as data.

Kept out of decoder logic on purpose: prices change often, and a table is
something a user can correct with a pull request without reading any code.

**Sources**, both checked **2026-08-18**:

* https://developers.openai.com/api/docs/pricing
* https://platform.claude.com/docs/en/about-claude/pricing

Entries are USD per **one million** tokens, ``(input, output)``, at base rates.
Batch (50% off), prompt-caching multipliers, fast mode, and the US data
residency multiplier (1.1x) are deliberately not modelled: a trace records one
call, and guessing which discount applied to it would produce a confident wrong
number. Costs here are therefore an upper bound on a cached or batched run.

A model string that is not in this table leaves ``cost_usd`` null. Guessing a
price is worse than admitting we do not know one: a wrong number in a cost
report is not obviously wrong, and a missing one is.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

#: The date the tables below were last checked against the pages above.
CHECKED = "2026-08-18"

#: model prefix -> (USD per 1M input tokens, USD per 1M output tokens)
#:
#: Matched by longest prefix, so dated and revisioned ids
#: (``gpt-4o-mini-2024-07-18``, ``claude-opus-4-5-20251101``) resolve without an
#: entry each.
PRICES: Dict[str, Tuple[float, float]] = {
    # -- OpenAI ---------------------------------------------------------
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.1": (1.25, 10.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "o4-mini": (1.10, 4.40),
    "o3": (2.00, 8.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    # -- Anthropic ------------------------------------------------------
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
}


def lookup(model: Optional[str]) -> Optional[Tuple[float, float]]:
    """Prices for ``model``, matching the longest registered prefix.

    Providers append dates and revisions, so an exact-match table would go
    stale the day after it was written.
    """
    if not model:
        return None
    name = model.strip().lower()
    # Strip a deployment prefix, e.g. Azure's "my-deployment/gpt-4o".
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    best: Optional[str] = None
    for prefix in PRICES:
        if name.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICES[best] if best else None


def cost_usd(
    model: Optional[str], tokens_in: Optional[int], tokens_out: Optional[int]
) -> Optional[float]:
    """Cost of one call, or None when the model or the token counts are unknown."""
    prices = lookup(model)
    if prices is None or (tokens_in is None and tokens_out is None):
        return None
    per_in, per_out = prices
    total = (tokens_in or 0) / 1_000_000 * per_in + (tokens_out or 0) / 1_000_000 * per_out
    return round(total, 8)
