"""Money is an int of cents everywhere."""
from decimal import ROUND_HALF_UP, Decimal


def pct(amount_cents: int, percent) -> int:
    """`percent` (0..100) of an amount, rounded half up: the house rounding (INC-2231)."""
    value = Decimal(amount_cents) * Decimal(str(percent)) / Decimal(100)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def percent(amount_cents: int, rate: float) -> int:
    """`rate` (0..1) of an amount, in cents. Kept for the staff rule only: new rules use `pct`."""
    return round(amount_cents * rate)


def split(amount_cents: int, parts: int) -> list[int]:
    """An amount split into `parts` shares that add up to it, the larger shares first."""
    share, left = divmod(amount_cents, parts)
    return [share + (1 if n < left else 0) for n in range(parts)]


def fmt(amount_cents: int) -> str:
    sign = "-" if amount_cents < 0 else ""
    cents = abs(amount_cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
