from tally.lines import amount, tags_of
from tally.money import pct
from tally.rules.base import Adjustment, Rule


class Clearance(Rule):
    name = "clearance"
    stage = "line"

    def apply_line(self, line, customer):
        if "clearance" in tags_of(line):
            return [Adjustment("clearance", -pct(amount(line), 30))]
        return []
