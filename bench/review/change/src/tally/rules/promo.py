from tally.lines import amount
from tally.money import pct
from tally.rules.base import Adjustment, Rule

#: Lines tagged promo get this percent off.
PROMO_PCT = 15


class Promo(Rule):
    """Lines tagged promo get 15% off that line."""

    name = "promo"
    stage = "line"

    def apply_line(self, line, customer):
        if "promo" in (line.get("tags") or "").lower():
            return [Adjustment("promo", -pct(amount(line), PROMO_PCT))]
        return []
