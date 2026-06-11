from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledgerlink.reconciler import (
    Invoice,
    Payment,
    parse_invoices,
    parse_payments,
    reconcile,
    write_exceptions_csv,
    write_json_summary,
)
from ledgerlink.cli import main


@pytest.fixture
def sample_invoices() -> list[Invoice]:
    return [
        Invoice("INV-001", "Alice Corp", date(2024, 1, 1), date(2024, 1, 31), Decimal("1000.00"), "open"),
        Invoice("INV-002", "Bob LLC", date(2024, 1, 5), date(2024, 2, 5), Decimal("2500.00"), "open"),
        Invoice("INV-003", "Alice Corp", date(2024, 1, 10), date(2024, 2, 10), Decimal("500.00"), "open"),
        Invoice("INV-004", "Charlie Inc", date(2024, 1, 15), date(2024, 2, 15), Decimal("750.00"), "voided"),
    ]


@pytest.fixture
def sample_payments() -> list[Payment]:
    return [
        Payment("PMT-001", date(2024, 1, 20), Decimal("1000.00"), "Payment for INV-001"),
        Payment("PMT-002", date(2024, 2, 1), Decimal("2500.00"), "Wire transfer", customer="Bob LLC"),
        Payment("PMT-003", date(2024, 2, 3), Decimal("500.00"), "Ref: INV-003"),
    ]


class TestNormalMatching:
    def test_memo_match(self, sample_invoices, sample_payments):
        result = reconcile(sample_invoices, sample_payments, date(2024, 3, 1))
        memo_matches = [m for m in result.matches if m.match_type == "memo"]
        assert len(memo_matches) == 2
        matched_ids = {m.invoice_id for m in memo_matches}
        assert "INV-001" in matched_ids
        assert "INV-003" in matched_ids

    def test_amount_fallback_match(self, sample_invoices, sample_payments):
        result = reconcile(sample_invoices, sample_payments, date(2024, 3, 1))
        amount_matches = [m for m in result.matches if m.match_type == "amount"]
        assert len(amount_matches) == 1
        assert amount_matches[0].invoice_id == "INV-002"
        assert amount_matches[0].payment_id == "PMT-002"


class TestMemoPriority:
    def test_memo_takes_precedence_over_amount(self):
        invoices = [
            Invoice("INV-100", "Acme", date(2024, 1, 1), date(2024, 2, 1), Decimal("500.00"), "open"),
            Invoice("INV-200", "Acme", date(2024, 1, 1), date(2024, 2, 1), Decimal("500.00"), "open"),
        ]
        payments = [
            Payment("PMT-A", date(2024, 1, 15), Decimal("500.00"), "For INV-200", customer="Acme"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        assert len(result.matches) == 1
        assert result.matches[0].invoice_id == "INV-200"
        assert result.matches[0].match_type == "memo"


class TestVoidedInvoiceSkip:
    def test_voided_invoices_not_matched(self, sample_invoices, sample_payments):
        result = reconcile(sample_invoices, sample_payments, date(2024, 3, 1))
        matched_invoice_ids = {m.invoice_id for m in result.matches}
        assert "INV-004" not in matched_invoice_ids

    def test_voided_not_reported_overdue(self):
        invoices = [
            Invoice("INV-V", "X Corp", date(2024, 1, 1), date(2024, 1, 10), Decimal("100.00"), "voided"),
        ]
        result = reconcile(invoices, [], date(2024, 3, 1))
        overdue = [e for e in result.exceptions if e.type == "overdue"]
        assert len(overdue) == 0


class TestDuplicatePayment:
    def test_duplicate_detected(self):
        invoices = [
            Invoice("INV-500", "Dup Co", date(2024, 1, 1), date(2024, 2, 1), Decimal("300.00"), "open"),
        ]
        payments = [
            Payment("PMT-D1", date(2024, 1, 20), Decimal("300.00"), "Pay INV-500"),
            Payment("PMT-D2", date(2024, 1, 21), Decimal("300.00"), "Pay INV-500"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        dupes = [e for e in result.exceptions if e.type == "duplicate"]
        assert len(dupes) == 1
        assert dupes[0].payment_id == "PMT-D2"


class TestOverdueDetection:
    def test_overdue_reported(self):
        invoices = [
            Invoice("INV-OD", "Late Co", date(2024, 1, 1), date(2024, 1, 15), Decimal("800.00"), "open"),
        ]
        result = reconcile(invoices, [], date(2024, 2, 1))
        overdue = [e for e in result.exceptions if e.type == "overdue"]
        assert len(overdue) == 1
        assert overdue[0].invoice_id == "INV-OD"
        assert overdue[0].amount == Decimal("800.00")

    def test_not_overdue_before_due_date(self):
        invoices = [
            Invoice("INV-OK", "OnTime Co", date(2024, 1, 1), date(2024, 3, 1), Decimal("100.00"), "open"),
        ]
        result = reconcile(invoices, [], date(2024, 2, 1))
        overdue = [e for e in result.exceptions if e.type == "overdue"]
        assert len(overdue) == 0


class TestUnmatchedPayment:
    def test_unmatched_payment_exception(self):
        invoices = [
            Invoice("INV-X", "Foo", date(2024, 1, 1), date(2024, 2, 1), Decimal("100.00"), "open"),
        ]
        payments = [
            Payment("PMT-Z", date(2024, 1, 20), Decimal("999.00"), "Mystery payment"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        unmatched = [e for e in result.exceptions if e.type == "unmatched"]
        assert len(unmatched) == 1
        assert unmatched[0].payment_id == "PMT-Z"


class TestUnderpaidOverpaid:
    def test_underpaid(self):
        invoices = [
            Invoice("INV-UP", "Under Co", date(2024, 1, 1), date(2024, 2, 1), Decimal("1000.00"), "open"),
        ]
        payments = [
            Payment("PMT-UP", date(2024, 1, 20), Decimal("800.00"), "Partial INV-UP"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        underpaid = [e for e in result.exceptions if e.type == "underpaid"]
        assert len(underpaid) == 1
        assert underpaid[0].amount == Decimal("200.00")

    def test_overpaid(self):
        invoices = [
            Invoice("INV-OP", "Over Co", date(2024, 1, 1), date(2024, 2, 1), Decimal("500.00"), "open"),
        ]
        payments = [
            Payment("PMT-OP", date(2024, 1, 20), Decimal("600.00"), "INV-OP payment"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        overpaid = [e for e in result.exceptions if e.type == "overpaid"]
        assert len(overpaid) == 1
        assert overpaid[0].amount == Decimal("100.00")


class TestCLIOutput:
    def test_cli_creates_output_files(self, tmp_path):
        inv_path = tmp_path / "invoices.csv"
        pmt_path = tmp_path / "payments.csv"
        json_path = tmp_path / "summary.json"
        exc_path = tmp_path / "exceptions.csv"

        with open(inv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["invoice_id", "customer", "issued_on", "due_on", "amount", "status"])
            w.writerow(["INV-001", "Test Co", "2024-01-01", "2024-01-31", "1000.00", "open"])

        with open(pmt_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["payment_id", "received_on", "amount", "memo", "customer"])
            w.writerow(["PMT-001", "2024-01-20", "1000.00", "Payment for INV-001", "Test Co"])

        exit_code = main([
            "reconcile",
            "--invoices", str(inv_path),
            "--payments", str(pmt_path),
            "--as-of", "2024-03-01",
            "--json-out", str(json_path),
            "--exceptions-out", str(exc_path),
        ])

        assert exit_code == 0
        assert json_path.exists()
        assert exc_path.exists()

        with open(json_path) as f:
            data = json.load(f)
        assert data["total_invoiced"] == "1000.00"
        assert data["total_received"] == "1000.00"
        assert data["exception_count"] == 0

    def test_cli_exceptions_csv_content(self, tmp_path):
        inv_path = tmp_path / "invoices.csv"
        pmt_path = tmp_path / "payments.csv"
        json_path = tmp_path / "summary.json"
        exc_path = tmp_path / "exceptions.csv"

        with open(inv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["invoice_id", "customer", "issued_on", "due_on", "amount", "status"])
            w.writerow(["INV-001", "Late Co", "2024-01-01", "2024-01-15", "500.00", "open"])

        with open(pmt_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["payment_id", "received_on", "amount", "memo"])

        exit_code = main([
            "reconcile",
            "--invoices", str(inv_path),
            "--payments", str(pmt_path),
            "--as-of", "2024-03-01",
            "--json-out", str(json_path),
            "--exceptions-out", str(exc_path),
        ])

        assert exit_code == 0
        with open(exc_path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["type"] == "overdue"
        assert rows[0]["invoice_id"] == "INV-001"


class TestAmountFallbackStrict:
    """Fallback only works with customer + unique amount match."""

    def test_amount_fallback_with_customer(self):
        """Payment with matching customer and unique amount -> match."""
        invoices = [
            Invoice("INV-F1", "Acme", date(2024, 1, 1), date(2024, 2, 1), Decimal("750.00"), "open"),
        ]
        payments = [
            Payment("PMT-F1", date(2024, 1, 20), Decimal("750.00"), "bank transfer", customer="Acme"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        amount_matches = [m for m in result.matches if m.match_type == "amount"]
        assert len(amount_matches) == 1
        assert amount_matches[0].invoice_id == "INV-F1"

    def test_no_customer_on_payment_stays_unmatched(self):
        """Payment without customer field cannot fallback-match."""
        invoices = [
            Invoice("INV-F2", "Acme", date(2024, 1, 1), date(2024, 2, 1), Decimal("750.00"), "open"),
        ]
        payments = [
            Payment("PMT-F2", date(2024, 1, 20), Decimal("750.00"), "bank transfer"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        assert len(result.matches) == 0
        unmatched = [e for e in result.exceptions if e.type == "unmatched"]
        assert len(unmatched) == 1

    def test_alice_bob_same_amount_no_mismatch(self):
        """Two customers with same-amount invoices — payment must not cross-match."""
        invoices = [
            Invoice("INV-A1", "Alice", date(2024, 1, 1), date(2024, 2, 1), Decimal("500.00"), "open"),
            Invoice("INV-B1", "Bob", date(2024, 1, 1), date(2024, 2, 1), Decimal("500.00"), "open"),
        ]
        payments = [
            Payment("PMT-AB", date(2024, 1, 20), Decimal("500.00"), "wire", customer="Alice"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        assert len(result.matches) == 1
        assert result.matches[0].invoice_id == "INV-A1"
        assert result.matches[0].match_type == "amount"
        # Bob's invoice stays outstanding
        assert result.customer_outstanding.get("Bob") == Decimal("500.00")

    def test_same_customer_same_amount_ambiguous_stays_unmatched(self):
        """Same customer, two invoices with identical amount — can't pick, leave unmatched."""
        invoices = [
            Invoice("INV-X1", "Alice", date(2024, 1, 1), date(2024, 2, 1), Decimal("500.00"), "open"),
            Invoice("INV-X2", "Alice", date(2024, 1, 5), date(2024, 2, 5), Decimal("500.00"), "open"),
        ]
        payments = [
            Payment("PMT-X", date(2024, 1, 20), Decimal("500.00"), "wire", customer="Alice"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        assert len(result.matches) == 0
        unmatched = [e for e in result.exceptions if e.type == "unmatched"]
        assert len(unmatched) == 1
        assert unmatched[0].payment_id == "PMT-X"
        # Both invoices still outstanding
        assert result.customer_outstanding["Alice"] == Decimal("1000.00")


class TestSummaryTotals:
    def test_customer_outstanding(self):
        invoices = [
            Invoice("INV-A", "Alice", date(2024, 1, 1), date(2024, 2, 1), Decimal("1000.00"), "open"),
            Invoice("INV-B", "Alice", date(2024, 1, 1), date(2024, 2, 1), Decimal("500.00"), "open"),
            Invoice("INV-C", "Bob", date(2024, 1, 1), date(2024, 2, 1), Decimal("300.00"), "open"),
        ]
        payments = [
            Payment("PMT-1", date(2024, 1, 20), Decimal("1000.00"), "INV-A payment", customer="Alice"),
        ]
        result = reconcile(invoices, payments, date(2024, 3, 1))
        assert result.customer_outstanding["Alice"] == Decimal("500.00")
        assert result.customer_outstanding["Bob"] == Decimal("300.00")
        assert result.total_outstanding == Decimal("800.00")
