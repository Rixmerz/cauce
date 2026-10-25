from tally.money import fmt, split
from tally.rules.bulk import Bulk


def test_split_adds_up_with_a_remainder():
    assert split(100, 3) == [34, 33, 33]


def test_negative_amounts_print_with_their_sign():
    assert fmt(-150) == "-1.50"


def test_bulk_starts_at_ten():
    assert Bulk().apply_line({"price": 100, "qty": 10, "tags": ""}, {})[0].cents == -50
