from dataclasses import dataclass


@dataclass(frozen=True)
class Adjustment:
    label: str
    cents: int  # negative for a discount


class Rule:
    """A pricing rule. `stage` is "line" (sees each line, before any order rule) or "order"
    (sees the subtotal after every line rule). `apply` returns adjustments and changes nothing
    it is given: the cart is shared by every rule."""

    name = ""
    stage = "line"

    def apply_line(self, line, customer):  # stage "line"
        return []

    def apply_order(self, subtotal_cents, cart, customer):  # stage "order"
        return []
