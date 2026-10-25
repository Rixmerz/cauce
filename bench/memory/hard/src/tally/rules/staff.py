from tally.money import percent
from tally.rules.base import Adjustment, Rule


class Staff(Rule):
    name = "staff"
    stage = "order"

    def apply_order(self, subtotal_cents, cart, customer):
        if customer.get("staff"):
            return [Adjustment("staff", -percent(subtotal_cents, 0.2))]
        return []
