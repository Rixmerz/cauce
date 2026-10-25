from common import cart, priced


def test_half_up():
    assert priced(cart(("a", 25, 10, ())), {}) == 237


def test_below_ten():
    assert priced(cart(("a", 25, 9, ())), {}) == 225


def test_before_order_rules():
    assert priced(cart(("a", 1000, 10, ())), {"staff": True}) == 7600


def test_with_clearance_on_the_line_amount():
    assert priced(cart(("a", 100, 10, ("clearance",))), {}) == 650
