from common import cart, priced, who


def test_silver_half_up():
    assert priced(cart(("a", 250, 1, "")), who("SILVER")) == 237


def test_with_staff_on_the_same_subtotal():
    assert priced(cart(("a", 1000, 1, "")), who("SILVER", staff=True)) == 750


def test_others_and_no_loyalty():
    assert priced(cart(("a", 250, 1, "")), who("GOLD")) == 250
    assert priced(cart(("a", 250, 1, "")), who(None)) == 250
