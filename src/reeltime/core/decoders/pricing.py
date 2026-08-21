"""Model pricing, as data.

Kept out of decoder logic on purpose: prices change often, and a table is
something a user can correct with a pull request without reading any code.

**Sources**:

* https://developers.openai.com/api/docs/pricing — checked **2026-08-18**
* https://platform.claude.com/docs/en/about-claude/pricing — checked **2026-08-18**
* https://aws.amazon.com/bedrock/pricing/ — checked **2026-08-21**. That page
  still renders its current-model tables client-side, so it remains unusable
  as a source for anything not in the served HTML.
* **AWS Price List Query API** — checked **2026-08-21**, and the source for
  every Bedrock row below. Public, unauthenticated, and machine-readable, which
  is what the pricing page is not:

      https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/region_index.json
      https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/<region>/index.json

  A SKU's ``usagetype`` is what disambiguates a rate: ``USE1-NovaLite-input-tokens``
  is the base on-demand price, while ``-batch``, ``-custom-model`` and
  ``-cross-region-global`` are different products at different rates. Reading a
  model's "price" without filtering on that is how you get a number that is
  half or 1.1x the real one.

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
CHECKED = "2026-08-21"

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
    # -- Amazon Bedrock -------------------------------------------------
    #
    # Bedrock model ids are their own namespace (`anthropic.claude-…`,
    # `amazon.nova-…`), so they cannot borrow the rows above -- and must not.
    # **Bedrock is a separate price list for the same models**, so aliasing
    # `anthropic.claude-…` to the `claude-…` row above is the obvious shortcut
    # and produces a confidently wrong number. A Bedrock id that is not in this
    # section therefore resolves to no price at all rather than falling through
    # to the first-party one -- there is a test asserting exactly that.
    #
    # Every row here is the **base on-demand rate in a US region**, read from
    # the Price List Query API and filtered to the plain `-input-tokens` /
    # `-output-tokens` usagetype. Rates are *not* uniform worldwide: the same
    # Nova Lite is $0.06/$0.24 in us-east-1 and us-west-2 and $0.078/$0.312 in
    # eu-central-1. A trace records a URL, not a bill, so US is the stated
    # basis and a non-US run is an underestimate rather than a guess.
    #
    # -- Amazon Nova (first generation) ---------------------------------
    # Verified 2026-08-20 against us-east-1 and us-west-2, which agree
    # exactly. Unambiguous because these models have a single in-region SKU:
    # no `-cross-region-global` variant exists for them, unlike Nova 2.0.
    "amazon.nova-micro": (0.035, 0.14),
    "amazon.nova-lite": (0.06, 0.24),
    "amazon.nova-pro": (0.80, 3.20),
    "amazon.nova-premier": (2.50, 12.50),
    #
    # **Nova 2.0 is deliberately absent**, for the same reason as Claude
    # below: `amazon.nova-2-lite-v1:0` is sold at $0.30/$2.50 per 1M through a
    # global cross-region profile and $0.33/$2.75 in-region — so one rate per
    # model id would be wrong, not merely incomplete. Its ids start
    # `amazon.nova-2-`, which does not prefix-match the rows above; that is
    # checked by a test rather than left to be noticed.
    #
    # -- Anthropic on Bedrock -------------------------------------------
    # Legacy, in-region-only models with a single unambiguous SKU each, both
    # re-verified 2026-08-20 against the Price List API.
    "anthropic.claude-instant": (0.80, 4.00),
    "anthropic.claude-v2:1": (8.00, 40.00),
    #
    # **Claude 3.5 Sonnet and newer are deliberately absent.** They are not
    # sold as in-region SKUs at all — they are served through cross-region
    # inference profiles, and the rate depends on which routing tier the
    # profile used: global vs geo vs in-region. Nothing in a recorded request
    # says which one answered it, so a single rate per model id would be
    # *wrong* rather than incomplete, and this table's whole discipline is
    # that a missing number is safer than a confident wrong one. Tokens still
    # populate; `cost_usd` stays null.
    #
    # This is also why `global.` is **not** in BEDROCK_REGION_PREFIXES below.
    #
    # -- Amazon Titan ---------------------------------------------------
    "amazon.titan-text-lite": (0.30, 0.40),
}

#: Cross-region inference profiles prefix the model id with a geography, so
#: ``us.anthropic.claude-…`` and ``anthropic.claude-…`` are the same model.
#: Stripped before lookup rather than duplicated into the table once per
#: geography.
#:
#: **``global.`` is deliberately not in this list.** It looks like one more
#: geography and is not: a global profile is a different *routing tier* at a
#: different rate (Nova 2.0 Lite is $0.30/1M global against $0.33/1M
#: in-region), so stripping it would silently price a global call at the
#: in-region rate. Left unstripped, ``global.amazon.nova-lite-v1:0`` matches
#: nothing and reports no cost, which is the honest answer.
#:
#: The geographies below are a real simplification too — a ``eu.`` profile is
#: genuinely more expensive than a ``us.`` one — and the table is US-based, so
#: a European run is under-reported. Stated in the module docstring rather than
#: silently absorbed.
BEDROCK_REGION_PREFIXES = ("us.", "eu.", "apac.", "us-gov.")


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
    for prefix in BEDROCK_REGION_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
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
