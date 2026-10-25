from tally.money import fmt, pct, split


def test_house_rounding_is_half_up():
    # Finance signed off on half up (INC-2231): 10% of 1.25 is 0.13.
    assert pct(125, 10) == 13


def test_split_adds_up():
    assert split(100, 4) == [25, 25, 25, 25]
    assert sum(split(900, 3)) == 900


def test_fmt():
    assert fmt(1999) == "19.99"
    assert fmt(5) == "0.05"
