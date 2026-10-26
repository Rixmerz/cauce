"""An order as CSV, for finance's import: one row per line, then the total."""
from pathlib import Path

from tally.checkout import line_total, price
from tally.money import fmt

HEADER = "item,qty,unit,total"


def rows(cart, customer) -> list[str]:
    out = [HEADER]
    for line in cart["lines"]:
        out.append(f"{line['name']},{line['qty']},{fmt(line['price'])},{fmt(line_total(line, customer))}")
    out.append(f"TOTAL,,,{fmt(price(cart, customer))}")
    return out


def export(cart, customer, out_dir) -> Path:
    """Write the order to `<out_dir>/<customer id>.csv` and return the path."""
    path = Path(out_dir) / f"{customer['id']}.csv"
    path.write_text("\n".join(rows(cart, customer)) + "\n")
    return path
