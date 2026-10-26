from tally.money import percent
from tally.rules.base import Adjustment, Rule

#: The order discount of each loyalty tier, in percent.
TIERS = {"gold": 10, "silver": 5}


class Loyalty(Rule):
    """Loyalty members get their tier's discount on the order."""

    name = "loyalty"
    stage = "order"

    def apply_order(self, subtotal_cents, cart, customer):
        loyalty = customer.get("loyalty") or {}
        off = TIERS.get(loyalty.get("tier"))
        if not off:
            return []
        return [Adjustment(f"loyalty {loyalty['tier']}", -percent(subtotal_cents, off / 100))]
