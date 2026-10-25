from common import cart, priced


def test_half_up():
    assert priced(cart(("a", 30, 1, ("promo",))), {}) == 25
    assert priced(cart(("a", 170, 1, ("promo",))), {}) == 144


def test_untagged():
    assert priced(cart(("a", 30, 1, ("other",))), {}) == 30


def test_before_order_rules():
    assert priced(cart(("a", 1000, 1, ("promo",))), {"staff": True}) == 680
