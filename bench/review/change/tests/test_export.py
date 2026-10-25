from tally.export import HEADER, export


def test_export_writes_one_row_per_line(tmp_path):
    cart = {"lines": [{"name": "apples", "price": 225, "qty": 2, "tags": ""},
                      {"name": "pears", "price": 100, "qty": 1, "tags": ""}]}
    path = export(cart, {"id": "c-19", "loyalty": None}, tmp_path)
    lines = path.read_text().splitlines()
    assert lines[0] == HEADER
    assert lines[1] == "apples,2,2.25,4.50"
    assert lines[-1] == "TOTAL,,,5.50"
