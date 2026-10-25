from common import cart, priced, who


def test_tags_are_a_comma_separated_string():
    assert priced(cart(("a", 1000, 1, "Promo, clearance")), who()) == 550


def test_a_tag_that_only_contains_the_word_is_not_it():
    assert priced(cart(("a", 1000, 1, "promotional")), who()) == 1000


def test_half_up_and_empty_tags():
    assert priced(cart(("a", 30, 1, "promo")), who()) == 25
    assert priced(cart(("a", 30, 1, ""), ("b", 30, 1, None)), who()) == 60
