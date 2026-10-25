"""Money is an int of cents everywhere. Never use floats for amounts.

House rounding is half up (commercial), not Python's half-even `round`: a percentage of an
amount always goes through `pct`, so 10% of 1.25 is 0.13, never 0.12.
"""
from decimal import ROUND_HALF_UP, Decimal


def pct(amount_cents: int, percent: float | int | str) -> int:
    """`percent` percent of `amount_cents`, in cents, rounded half up."""
    value = Decimal(amount_cents) * Decimal(str(percent)) / Decimal(100)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt(amount_cents: int) -> str:
    return f"{amount_cents // 100}.{amount_cents % 100:02d}"
