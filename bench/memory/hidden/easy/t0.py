from common import cart, priced


def test_half_up():
    assert priced(cart(("a", 125, 1, ())), {"tier": "gold"}) == 112


def test_not_gold():
    assert priced(cart(("a", 125, 1, ())), {"tier": "silver"}) == 125


def test_on_subtotal_after_line_rules():
    assert priced(cart(("a", 1000, 1, ("clearance",))), {"tier": "gold"}) == 630


def test_with_staff_both_on_subtotal():
    assert priced(cart(("a", 1000, 1, ())), {"tier": "gold", "staff": True}) == 700
