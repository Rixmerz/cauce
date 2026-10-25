from common import cart, priced


def test_threshold_inclusive_and_half_up():
    assert priced(cart(("a", 10000, 1, ())), {}) == 9250
    assert priced(cart(("a", 10020, 1, ())), {}) == 9268


def test_below():
    assert priced(cart(("a", 9999, 1, ())), {}) == 9999


def test_threshold_reads_the_subtotal_after_line_rules():
    assert priced(cart(("a", 12000, 1, ("clearance",))), {}) == 8400
