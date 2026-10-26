"""A printed receipt: one row per line and per adjustment, then the total."""
from tally.checkout import enabled, price, subtotal_of
from tally.lines import amount
from tally.money import fmt

#: Characters per row on the till's printer.
WIDTH = 32


def row(label: str, cents: int) -> str:
    """A label on the left and an amount on the right, `WIDTH` characters in all."""
    text = fmt(cents)
    room = WIDTH - len(text) - 1
    return f"{label[:room]:<{room}} {text}"


def receipt(cart, customer) -> str:
    rows = []
    for line in cart["lines"]:
        rows.append(row(f"{line['qty']} x {line['name']}", amount(line)))
        for rule in enabled("line"):
            for adj in rule.apply_line(line, customer):
                rows.append(row(f"  {adj.label}", adj.cents))
    subtotal = subtotal_of(cart, customer)
    rows.append(row("SUBTOTAL", subtotal))
    for rule in enabled("order"):
        for adj in rule.apply_order(subtotal, cart, customer):
            rows.append(row(adj.label, adj.cents))
    rows.append("-" * WIDTH)
    rows.append(row("TOTAL", price(cart, customer)))
    return "\n".join(rows)
