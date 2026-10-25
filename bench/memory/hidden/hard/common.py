import copy

from tally.checkout import price


def cart(*lines):
    return {"lines": [dict(name=n, price=p, qty=q, tags=t) for n, p, q, t in lines]}


def who(tier=None, since="2023-01-01", staff=False):
    return {"id": "c-1", "staff": staff, "loyalty": None if tier is None else {"tier": tier, "since": since}}


def priced(c, customer):
    before = copy.deepcopy((c, customer))
    total = price(c, customer)
    assert (c, customer) == before, "a rule changed the cart or the customer"
    return total
