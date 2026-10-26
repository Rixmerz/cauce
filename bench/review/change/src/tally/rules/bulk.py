from tally.lines import amount
from tally.money import pct
from tally.rules.base import Adjustment, Rule


class Bulk(Rule):
    """A line of 10 or more of one item gets 5% off that line."""

    name = "bulk"
    stage = "line"
    MIN_QTY = 10

    def apply_line(self, line, customer):
        if line["qty"] > self.MIN_QTY:
            return [Adjustment("bulk", -pct(amount(line), 5))]
        return []
