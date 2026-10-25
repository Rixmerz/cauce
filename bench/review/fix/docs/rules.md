# Pricing rules

Rules run in two stages. Line rules see one cart line at a time; order rules see the
subtotal after every line rule. `src/tally/rules.cfg` lists the rules that run, in order
within their stage: a rule that is not listed never runs.

| Rule | Stage | What it does |
|---|---|---|
| clearance | line | Lines tagged clearance get 30% off |
| promo | line | Lines tagged promo get 15% off |
| bulk | line | A line of 10 or more of one item gets 5% off |
| staff | order | Staff get 20% off the order |
| loyalty | order | Gold members get 10% off the order, silver 5% |

Every percentage is taken with `money.pct`, rounded half up (INC-2231). Order rules all
see the same subtotal: they do not stack on each other.

## Receipts and exports

`receipt.receipt(cart, customer)` prints a receipt for the till, 32 characters wide.
`export.export(cart, customer, out_dir)` writes the order as CSV for finance's import.

An item's name is written to the CSV as it is: names must not contain a comma.
