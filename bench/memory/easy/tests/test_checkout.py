from tally.checkout import price


def cart(*lines):
    return {"lines": [dict(name=n, price=p, qty=q, tags=t) for n, p, q, t in lines]}


def test_plain():
    assert price(cart(("a", 250, 2, ())), {}) == 500


def test_clearance_and_staff():
    total = price(cart(("a", 1000, 1, ("clearance",)), ("b", 500, 1, ())), {"staff": True})
    assert total == 960  # 700 + 500 = 1200, then 20% off
