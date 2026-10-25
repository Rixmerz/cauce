from tally.money import pct
from tally.rules.base import Adjustment, Rule


class Staff(Rule):
    """Staff get 20% off the order."""

    name = "staff"
    stage = "order"

    def apply_order(self, subtotal_cents, cart, customer):
        if customer.get("staff"):
            return [Adjustment("staff", -pct(subtotal_cents, 20))]
        return []
