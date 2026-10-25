import copy

from tally.checkout import price


def cart(*lines):
    return {"lines": [dict(name=n, price=p, qty=q, tags=list(t)) for n, p, q, t in lines]}


def priced(c, customer):
    before = copy.deepcopy((c, customer))
    total = price(c, customer)
    assert (c, customer) == before, "a rule changed the cart or the customer"
    return total
