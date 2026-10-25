from tally.money import pct
from tally.rules.base import Adjustment, Rule


class Clearance(Rule):
    """Lines tagged clearance get 30% off."""

    name = "clearance"
    stage = "line"

    def apply_line(self, line, customer):
        if "clearance" in line.get("tags", ()):
            return [Adjustment("clearance", -pct(line["price"] * line["qty"], 30))]
        return []
