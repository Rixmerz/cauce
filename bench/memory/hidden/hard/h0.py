from common import cart, priced, who


def test_gold_half_up():
    assert priced(cart(("a", 125, 1, "")), who("GOLD")) == 112


def test_others_and_no_loyalty():
    assert priced(cart(("a", 125, 1, "")), who("SILVER")) == 125
    assert priced(cart(("a", 125, 1, "")), who(None)) == 125


def test_on_subtotal_after_line_rules():
    assert priced(cart(("a", 1000, 1, "Clearance")), who("GOLD")) == 630
