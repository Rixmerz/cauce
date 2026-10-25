from tally.money import pct


def test_house_rounding_is_half_up():
    # Finance signed off on half up (INC-2231): 10% of 1.25 is 0.13.
    assert pct(125, 10) == 13
