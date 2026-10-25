from common import cart, priced, who


def test_members_since_before_2022_half_up():
    assert priced(cart(("a", 1000, 1, "")), who("SILVER", since="2021-03-01")) == 975
    assert priced(cart(("a", 1020, 1, "")), who("GOLD", since="2021-12-31")) == 994


def test_since_2022_or_no_loyalty():
    assert priced(cart(("a", 1000, 1, "")), who("GOLD", since="2022-01-01")) == 1000
    assert priced(cart(("a", 1000, 1, "")), who(None)) == 1000
