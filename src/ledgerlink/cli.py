from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from ledgerlink.reconciler import (
    parse_invoices,
    parse_payments,
    reconcile,
    write_exceptions_csv,
    write_json_summary,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledgerlink", description="Invoice-to-payment reconciliation tool")
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("reconcile", help="Reconcile invoices against payments")
    rec.add_argument("--invoices", required=True, type=Path, help="Path to invoices CSV")
    rec.add_argument("--payments", required=True, type=Path, help="Path to payments CSV")
    rec.add_argument("--as-of", required=True, type=date.fromisoformat, help="Reference date (YYYY-MM-DD)")
    rec.add_argument("--json-out", required=True, type=Path, help="Output JSON summary path")
    rec.add_argument("--exceptions-out", required=True, type=Path, help="Output exceptions CSV path")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "reconcile":
        parser.print_help()
        return 1

    invoices = parse_invoices(args.invoices)
    payments = parse_payments(args.payments)
    result = reconcile(invoices, payments, args.as_of)

    write_json_summary(result, args.json_out)
    write_exceptions_csv(result, args.exceptions_out)

    print(f"Reconciliation complete: {result.exception_count} exception(s) found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
