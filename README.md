# LedgerLink

CLI tool for reconciling invoice ledgers with payment records, built for small accounting teams.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

```bash
ledgerlink reconcile \
  --invoices invoices.csv \
  --payments payments.csv \
  --as-of 2024-03-01 \
  --json-out summary.json \
  --exceptions-out exceptions.csv
```

### Input formats

**invoices.csv** — columns: `invoice_id, customer, issued_on, due_on, amount, status`

Status values: `open`, `paid`, `voided`, `cancelled`

**payments.csv** — columns: `payment_id, received_on, amount, memo` (required), `customer` (optional)

The optional `customer` column enables stricter amount-based fallback matching.

### Matching rules

1. **Memo match** — if the payment memo contains an invoice ID, link them directly.
2. **Amount fallback** (strict) — for unmatched payments, match by exact amount only when:
   - The payment carries a `customer` value, AND
   - Exactly one open invoice for that customer has the matching amount.

   If the payment has no `customer`, or multiple invoices for the same customer share the same amount, the payment is left as **unmatched** (no guessing). The corresponding invoices remain in outstanding/overdue statistics.

Voided/cancelled invoices are excluded from matching entirely.

### Exceptions detected

| Type | Meaning |
|------|---------|
| overdue | Invoice past due date with no matching payment |
| underpaid | Total payments less than invoice amount |
| overpaid | Total payments exceed invoice amount |
| duplicate | Multiple payments matched to the same invoice |
| unmatched | Payment that cannot be linked to any invoice |

### Output

- **summary.json** — total invoiced, received, outstanding, exception count, per-customer outstanding breakdown, and full exception list.
- **exceptions.csv** — one row per exception for spreadsheet review.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```
