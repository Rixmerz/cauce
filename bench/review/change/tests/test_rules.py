from tally.checkout import price
from tally.rules.bulk import Bulk
from tally.rules.loyalty import Loyalty
from tally.rules.promo import Promo


def line(price_, qty, tags=""):
    return {"name": "x", "price": price_, "qty": qty, "tags": tags}


def test_promo_takes_15_percent():
    assert [a.cents for a in Promo().apply_line(line(1000, 1, "promo"), {})] == [-150]
    assert Promo().apply_line(line(1000, 1, "sale"), {}) == []


def test_bulk_takes_5_percent_on_big_lines():
    assert [a.cents for a in Bulk().apply_line(line(100, 12), {})] == [-60]
    assert Bulk().apply_line(line(100, 3), {}) == []


def test_loyalty_tiers():
    gold = {"loyalty": {"tier": "gold", "since": "2020-01-01"}}
    silver = {"loyalty": {"tier": "silver", "since": "2020-01-01"}}
    assert [a.cents for a in Loyalty().apply_order(10000, {}, gold)] == [-1000]
    assert [a.cents for a in Loyalty().apply_order(10000, {}, silver)] == [-500]
    assert Loyalty().apply_order(10000, {}, {"loyalty": None}) == []


def test_promo_line_in_a_cart():
    cart = {"lines": [line(2000, 1, "Promo, new")]}
    assert price(cart, {"loyalty": None}) == 1700
