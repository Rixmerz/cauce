from tally.receipt import WIDTH, receipt, row


def test_row_is_the_printer_s_width():
    assert len(row("2 x apples", 450)) == WIDTH
    assert row("2 x apples", 450).endswith(" 4.50")


def test_receipt_ends_with_the_total():
    cart = {"lines": [{"name": "apples", "price": 225, "qty": 2, "tags": ""}]}
    text = receipt(cart, {"loyalty": None})
    assert text.splitlines()[-1].endswith(" 4.50")
    assert "SUBTOTAL" in text
