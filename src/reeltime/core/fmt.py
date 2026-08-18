"""Display helpers shared by the CLI and the run summary."""

from __future__ import annotations

from typing import Optional


def usd(amount: Optional[float]) -> str:
    """Format a cost without ever implying it was free.

    A single LLM call routinely costs a few millionths of a dollar, and both
    ``$0.00`` and ``$0.0000`` read as "nothing happened" for a number the user
    may well be trying to act on. Below the point where four decimals stop
    saying anything, say so explicitly instead.
    """
    if amount is None:
        return "–"
    if amount == 0:
        return "$0.00"
    if abs(amount) < 0.0001:
        return "<$0.0001"
    if abs(amount) < 0.01:
        return "${:.4f}".format(amount)
    return "${:.2f}".format(amount)
