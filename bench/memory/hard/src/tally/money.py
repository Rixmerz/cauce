"""Money is an int of cents everywhere."""
from decimal import ROUND_HALF_UP, Decimal


def pct(amount_cents: int, percent) -> int:
    value = Decimal(amount_cents) * Decimal(str(percent)) / Decimal(100)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def percent(amount_cents: int, rate: float) -> int:
    """`rate` (0..1) of an amount, in cents."""
    return round(amount_cents * rate)


def fmt(amount_cents: int) -> str:
    return f"{amount_cents // 100}.{amount_cents % 100:02d}"
