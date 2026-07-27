"""Token -> USD conversion, applied at render time.

List prices, USD per million tokens (verified 2026-07-27), one table per
provider. Cache multipliers are relative to the model's base input rate:

    5-minute cache write : 1.25x input
    1-hour cache write   : 2.00x input
    cache read           : 0.10x input

These are notional API list prices, not real invoices -- both sources
(`claude_code_local`, `codex_local`) cover subscription usage that is never
actually billed per token.
"""

from __future__ import annotations

MTOK = 1_000_000

CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00
CACHE_READ_MULT = 0.10

# model id -> (input $/MTok, output $/MTok)
ANTHROPIC_RATES: dict[str, tuple[float, float]] = {
    "claude-fable-5":     (10.00, 50.00),
    "claude-mythos-5":    (10.00, 50.00),
    "claude-opus-5":      ( 5.00, 25.00),
    "claude-opus-4-8":    ( 5.00, 25.00),
    "claude-opus-4-7":    ( 5.00, 25.00),
    "claude-opus-4-6":    ( 5.00, 25.00),
    "claude-opus-4-5":    ( 5.00, 25.00),
    "claude-opus-4-1":    (15.00, 75.00),
    "claude-sonnet-5":    ( 3.00, 15.00),
    "claude-sonnet-4-6":  ( 3.00, 15.00),
    "claude-sonnet-4-5":  ( 3.00, 15.00),
    "claude-sonnet-4-0":  ( 3.00, 15.00),
    "claude-haiku-4-5":   ( 1.00,  5.00),
}

# model id -> (input $/MTok, output $/MTok), per platform.openai.com/docs/pricing.
# Cached input is a flat 0.10x input across every model below, same as
# CACHE_READ_MULT, so it needs no separate table.
OPENAI_RATES: dict[str, tuple[float, float]] = {
    "gpt-5.6-sol":    ( 5.00,  30.00),
    "gpt-5.6-terra":  ( 2.50,  15.00),
    "gpt-5.6-luna":   ( 1.00,   6.00),
    "gpt-5.5":        ( 5.00,  30.00),
    "gpt-5.5-pro":    (30.00, 180.00),
    "gpt-5.4":        ( 2.50,  15.00),
    "gpt-5.4-mini":   ( 0.75,   4.50),
    "gpt-5.4-nano":   ( 0.20,   1.25),
    "gpt-5.4-pro":    (30.00, 180.00),
    "gpt-5.3-codex":  ( 1.75,  14.00),
}

PROVIDER_RATES: dict[str, dict[str, tuple[float, float]]] = {
    "anthropic": ANTHROPIC_RATES,
    "openai": OPENAI_RATES,
}

# Promotional pricing that applies only inside a date window. Sonnet 5 launched
# at an introductory rate; without this, July is over-costed.
DATED_OVERRIDES: list[tuple[str, str, str, tuple[float, float]]] = [
    # (model, start_day_inclusive, end_day_inclusive, (input, output))
    ("claude-sonnet-5", "2026-01-01", "2026-08-31", (2.00, 10.00)),
]

# Models that cost nothing to run but are still worth counting.
FREE_PREFIXES = ("ollama/", "qwen", "llama", "mistral", "gemma", "deepseek")


def normalize_model(model: str) -> str:
    """Strip the date suffix Claude Code sometimes records.

    `claude-sonnet-4-5-20250929` -> `claude-sonnet-4-5`. Applied only when the
    trailing segment is an 8-digit date, so `claude-opus-4-5` is left alone.
    """
    if not model:
        return "unknown"
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
        return parts[0]
    return model


def rates_for(model: str, day: str, provider: str = "anthropic") -> tuple[float, float] | None:
    """Return (input, output) $/MTok for a model on a given day, or None if we
    have no price for it. None is meaningful: the caller reports the tokens and
    omits the dollars rather than inventing a number."""
    m = normalize_model(model)
    for om, start, end, rate in DATED_OVERRIDES:
        if om == m and start <= day <= end:
            return rate
    return PROVIDER_RATES.get(provider, {}).get(m)


def is_free(model: str) -> bool:
    m = normalize_model(model).lower()
    return any(m.startswith(p) for p in FREE_PREFIXES)


def cost_usd(row) -> float | None:
    """Cost for one usage row. `row` needs the token columns plus `model`,
    `day`, and `provider`.

    Returns None when the model has no known price -- callers must surface that
    as "unpriced", never as zero.
    """
    model = row["model"]
    if is_free(model):
        return 0.0

    provider = row["provider"] if "provider" in row.keys() else "anthropic"
    rate = rates_for(model, row["day"], provider)
    if rate is None:
        return None

    inp, out = rate
    total = (
        row["input_tokens"] * inp
        + row["output_tokens"] * out
        + row["cache_write_5m_tokens"] * inp * CACHE_WRITE_5M_MULT
        + row["cache_write_1h_tokens"] * inp * CACHE_WRITE_1H_MULT
        + row["cache_read_tokens"] * inp * CACHE_READ_MULT
    )
    return total / MTOK
