import json
from pathlib import Path

from tally.checkout import price

CUSTOMERS = json.loads((Path(__file__).parent / "fixtures" / "customers.json").read_text())


def cart(*lines):
    return {"lines": [dict(name=n, price=p, qty=q, tags=t) for n, p, q, t in lines]}


def test_plain():
    assert price(cart(("a", 250, 2, "")), CUSTOMERS["cy"]) == 500


def test_clearance_and_staff():
    total = price(cart(("a", 1000, 1, "Clearance"), ("b", 500, 1, "")), CUSTOMERS["ben"])
    assert total == 960
