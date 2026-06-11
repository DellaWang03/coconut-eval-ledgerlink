from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TextIO


@dataclass
class Invoice:
    invoice_id: str
    customer: str
    issued_on: date
    due_on: date
    amount: Decimal
    status: str


@dataclass
class Payment:
    payment_id: str
    received_on: date
    amount: Decimal
    memo: str
    customer: str | None = None


@dataclass
class MatchResult:
    payment_id: str
    invoice_id: str | None
    match_type: str  # "memo", "amount", "unmatched"
    amount: Decimal


@dataclass
class Exception_:
    type: str  # overdue, underpaid, overpaid, duplicate, unmatched
    invoice_id: str | None
    payment_id: str | None
    customer: str | None
    amount: Decimal
    detail: str


@dataclass
class ReconciliationResult:
    matches: list[MatchResult] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)
    total_invoiced: Decimal = Decimal(0)
    total_received: Decimal = Decimal(0)
    total_outstanding: Decimal = Decimal(0)
    exception_count: int = 0
    customer_outstanding: dict[str, Decimal] = field(default_factory=dict)


def parse_invoices(path: Path) -> list[Invoice]:
    rows: list[Invoice] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(Invoice(
                invoice_id=row["invoice_id"].strip(),
                customer=row["customer"].strip(),
                issued_on=date.fromisoformat(row["issued_on"].strip()),
                due_on=date.fromisoformat(row["due_on"].strip()),
                amount=Decimal(row["amount"].strip()),
                status=row["status"].strip().lower(),
            ))
    return rows


def parse_payments(path: Path) -> list[Payment]:
    rows: list[Payment] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            customer_raw = row.get("customer", "")
            customer = customer_raw.strip() if customer_raw else None
            rows.append(Payment(
                payment_id=row["payment_id"].strip(),
                received_on=date.fromisoformat(row["received_on"].strip()),
                amount=Decimal(row["amount"].strip()),
                memo=row["memo"].strip(),
                customer=customer or None,
            ))
    return rows


def reconcile(invoices: list[Invoice], payments: list[Payment], as_of: date) -> ReconciliationResult:
    result = ReconciliationResult()

    active_invoices = [inv for inv in invoices if inv.status not in ("voided", "cancelled")]
    open_invoices = {inv.invoice_id: inv for inv in active_invoices if inv.status != "paid"}
    matched_invoice_ids: dict[str, list[str]] = {}  # invoice_id -> list of payment_ids
    matched_payment_ids: set[str] = set()

    # Phase 1: memo-based matching
    for pmt in payments:
        for inv_id, inv in open_invoices.items():
            if inv_id in pmt.memo:
                result.matches.append(MatchResult(
                    payment_id=pmt.payment_id,
                    invoice_id=inv_id,
                    match_type="memo",
                    amount=pmt.amount,
                ))
                matched_invoice_ids.setdefault(inv_id, []).append(pmt.payment_id)
                matched_payment_ids.add(pmt.payment_id)
                break

    # Phase 2: amount-based fallback for unmatched payments
    # Only match when the payment carries a customer AND exactly one open
    # invoice for that customer has the same amount (no guessing).
    for pmt in payments:
        if pmt.payment_id in matched_payment_ids:
            continue
        if not pmt.customer:
            continue
        candidates = [
            inv for inv in open_invoices.values()
            if inv.invoice_id not in matched_invoice_ids
            and inv.customer == pmt.customer
            and inv.amount == pmt.amount
        ]
        if len(candidates) == 1:
            inv = candidates[0]
            result.matches.append(MatchResult(
                payment_id=pmt.payment_id,
                invoice_id=inv.invoice_id,
                match_type="amount",
                amount=pmt.amount,
            ))
            matched_invoice_ids.setdefault(inv.invoice_id, []).append(pmt.payment_id)
            matched_payment_ids.add(pmt.payment_id)

    # Detect exceptions
    # Duplicate payments: multiple payments matched to one invoice
    for inv_id, pmt_ids in matched_invoice_ids.items():
        if len(pmt_ids) > 1:
            for pid in pmt_ids[1:]:
                pmt = next(p for p in payments if p.payment_id == pid)
                inv = open_invoices[inv_id]
                result.exceptions.append(Exception_(
                    type="duplicate",
                    invoice_id=inv_id,
                    payment_id=pid,
                    customer=inv.customer,
                    amount=pmt.amount,
                    detail=f"Duplicate payment for invoice {inv_id}",
                ))

    # Underpaid / overpaid
    for inv_id, pmt_ids in matched_invoice_ids.items():
        inv = open_invoices[inv_id]
        total_paid = sum(
            next(p for p in payments if p.payment_id == pid).amount
            for pid in pmt_ids
        )
        diff = total_paid - inv.amount
        if diff < 0:
            result.exceptions.append(Exception_(
                type="underpaid",
                invoice_id=inv_id,
                payment_id=pmt_ids[0],
                customer=inv.customer,
                amount=abs(diff),
                detail=f"Underpaid by {abs(diff)}",
            ))
        elif diff > 0:
            result.exceptions.append(Exception_(
                type="overpaid",
                invoice_id=inv_id,
                payment_id=pmt_ids[0],
                customer=inv.customer,
                amount=diff,
                detail=f"Overpaid by {diff}",
            ))

    # Unmatched payments
    for pmt in payments:
        if pmt.payment_id not in matched_payment_ids:
            result.exceptions.append(Exception_(
                type="unmatched",
                invoice_id=None,
                payment_id=pmt.payment_id,
                customer=None,
                amount=pmt.amount,
                detail=f"Payment {pmt.payment_id} could not be matched",
            ))

    # Overdue unpaid
    for inv in active_invoices:
        if inv.invoice_id in matched_invoice_ids:
            continue
        if inv.status == "paid":
            continue
        if inv.due_on < as_of:
            result.exceptions.append(Exception_(
                type="overdue",
                invoice_id=inv.invoice_id,
                payment_id=None,
                customer=inv.customer,
                amount=inv.amount,
                detail=f"Invoice {inv.invoice_id} overdue since {inv.due_on}",
            ))

    # Summaries
    result.total_invoiced = sum((inv.amount for inv in active_invoices), Decimal(0))
    result.total_received = sum((pmt.amount for pmt in payments), Decimal(0))

    # Customer outstanding: open invoices not fully paid
    for inv in active_invoices:
        if inv.status == "paid":
            continue
        paid_amount = Decimal(0)
        if inv.invoice_id in matched_invoice_ids:
            paid_amount = sum(
                next(p for p in payments if p.payment_id == pid).amount
                for pid in matched_invoice_ids[inv.invoice_id]
            )
        outstanding = inv.amount - paid_amount
        if outstanding > 0:
            result.customer_outstanding[inv.customer] = (
                result.customer_outstanding.get(inv.customer, Decimal(0)) + outstanding
            )

    result.total_outstanding = sum(result.customer_outstanding.values(), Decimal(0))
    result.exception_count = len(result.exceptions)

    return result


def write_json_summary(result: ReconciliationResult, path: Path) -> None:
    data = {
        "total_invoiced": str(result.total_invoiced),
        "total_received": str(result.total_received),
        "total_outstanding": str(result.total_outstanding),
        "exception_count": result.exception_count,
        "customer_outstanding": {k: str(v) for k, v in result.customer_outstanding.items()},
        "exceptions": [
            {
                "type": e.type,
                "invoice_id": e.invoice_id,
                "payment_id": e.payment_id,
                "customer": e.customer,
                "amount": str(e.amount),
                "detail": e.detail,
            }
            for e in result.exceptions
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_exceptions_csv(result: ReconciliationResult, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["type", "invoice_id", "payment_id", "customer", "amount", "detail"])
        for e in result.exceptions:
            writer.writerow([e.type, e.invoice_id or "", e.payment_id or "", e.customer or "", str(e.amount), e.detail])
