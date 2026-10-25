from pathlib import Path

from tally.rules import RULES

CFG = Path(__file__).with_name("rules.cfg")


def enabled():
    names = [ln.strip() for ln in CFG.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return [RULES[n] for n in names if n in RULES]


def price(cart, customer):
    """The cart's total in cents: each line's price * qty, line rules on each line, then
    order rules on the subtotal."""
    rules = enabled()
    subtotal = 0
    for line in cart["lines"]:
        amount = line["price"] * line["qty"]
        for rule in rules:
            if rule.stage == "line":
                amount += sum(a.cents for a in rule.apply_line(line, customer))
        subtotal += amount
    total = subtotal
    for rule in rules:
        if rule.stage == "order":
            total += sum(a.cents for a in rule.apply_order(subtotal, cart, customer))
    return total
