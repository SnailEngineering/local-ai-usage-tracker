"""Token -> USD conversion, applied at render time.

Anthropic list prices, USD per million tokens (verified 2026-07-27). Cache
multipliers are relative to the model's base input rate:

    5-minute cache write : 1.25x input
    1-hour cache write   : 2.00x input
    cache read           : 0.10x input

OpenAI is deliberately absent. Its Costs endpoint reports real billed dollars,
so we store those verbatim rather than re-deriving them from a price table we
would have to keep in sync.
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


def rates_for(model: str, day: str) -> tuple[float, float] | None:
    """Return (input, output) $/MTok for a model on a given day, or None if we
    have no price for it. None is meaningful: the caller reports the tokens and
    omits the dollars rather than inventing a number."""
    m = normalize_model(model)
    for om, start, end, rate in DATED_OVERRIDES:
        if om == m and start <= day <= end:
            return rate
    return ANTHROPIC_RATES.get(m)


def is_free(model: str) -> bool:
    m = normalize_model(model).lower()
    return any(m.startswith(p) for p in FREE_PREFIXES)


def cost_usd(row) -> float | None:
    """Cost for one usage row. `row` needs the token columns plus `model`,
    `day`, and `reported_cost_usd`.

    Returns None when the model has no known price -- callers must surface that
    as "unpriced", never as zero.
    """
    reported = row["reported_cost_usd"] if "reported_cost_usd" in row.keys() else None
    if reported is not None:
        return float(reported)

    model = row["model"]
    if is_free(model):
        return 0.0

    rate = rates_for(model, row["day"])
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
