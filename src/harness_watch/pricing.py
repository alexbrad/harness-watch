"""API list prices, USD per million tokens, and context windows.

Source: Claude API reference as of 2026-10-06. Sessions on a Claude subscription aren't
billed per token, so the dashboard shows this as an API-equivalent estimate.
Cache writes are priced from input: 1.25x for the 5-minute TTL, 2x for the 1-hour TTL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    context_window: int = 1_000_000


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.00, 50.00, 0.25),
    "claude-fable-5": Price(10.00, 50.00, 1.00),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
    "claude-opus-5": Price(5.00, 25.00, 0.50),
    "claude-opus-4-8": Price(5.00, 25.00, 0.50),
    "claude-opus-4-7": Price(5.00, 25.00, 0.50),
    "claude-opus-4-6": Price(5.00, 25.00, 0.50),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20),
    "claude-sonnet-5": Price(2.00, 10.00, 0.20),
    "claude-sonnet-4-6": Price(3.00, 15.00, 0.30),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01),
    "claude-haiku-4-5": Price(1.00, 5.00, 0.10, context_window=200_000),
}


def lookup(model: str | None) -> Price | None:
    """Price for a model id, tolerating suffixes like `[1m]` or a date."""
    if not model:
        return None
    base = model.split("[", 1)[0]
    # Longest prefix first so "claude-opus-5-5" isn't matched as "claude-opus-5".
    for key in sorted(PRICES, key=len, reverse=True):
        if base == key or base.startswith(key + "-"):
            return PRICES[key]
    return None


COST_PARTS = ("input", "cache_read", "cache_write", "output")


def cost_parts(usage: dict, model: str | None) -> dict[str, float] | None:
    """Cost in USD split into uncached input, cache reads, cache writes and output."""
    price = lookup(model)
    if price is None:
        return None
    cache_write = usage.get("cache_creation_input_tokens") or 0
    breakdown = usage.get("cache_creation") or {}
    write_1h = breakdown.get("ephemeral_1h_input_tokens") or 0
    write_5m = cache_write - write_1h
    per_mtok = {
        "input": (usage.get("input_tokens") or 0) * price.input,
        "cache_read": (usage.get("cache_read_input_tokens") or 0) * price.cache_read,
        "cache_write": write_5m * price.input * 1.25 + write_1h * price.input * 2.0,
        "output": (usage.get("output_tokens") or 0) * price.output,
    }
    return {k: v / 1_000_000 for k, v in per_mtok.items()}


def cost_usd(usage: dict, model: str | None) -> float | None:
    parts = cost_parts(usage, model)
    return None if parts is None else sum(parts.values())
