from pathlib import Path

from tally.lines import amount
from tally.rules import RULES

CFG = Path(__file__).with_name("rules.cfg")


def enabled(stage=None):
    """The rules rules.cfg lists, in its order; only those of `stage` when one is given."""
    names = [ln.strip() for ln in CFG.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    rules = [RULES[n] for n in names if n in RULES]
    return [r for r in rules if stage is None or r.stage == stage]


def line_total(line, customer, rules=None) -> int:
    """One line's amount after the line rules."""
    total = amount(line)
    for rule in enabled("line") if rules is None else rules:
        total += sum(a.cents for a in rule.apply_line(line, customer))
    return total


def subtotal_of(cart, customer) -> int:
    """The sum of the lines after every line rule: what the order rules see."""
    rules = enabled("line")
    return sum(line_total(line, customer, rules) for line in cart["lines"])


def price(cart, customer):
    """The cart's total in cents: each line's price * qty, line rules on each line, then
    order rules on the subtotal."""
    subtotal = subtotal_of(cart, customer)
    total = subtotal
    for rule in enabled("order"):
        total += sum(a.cents for a in rule.apply_order(subtotal, cart, customer))
    return total
