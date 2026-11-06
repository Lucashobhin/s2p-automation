"""Prove the database itself rejects bad Purchase Requisitions.

Run from backend/ (after seed_items):  python -m scripts.check_pr_constraints
It creates one temporary PR (key 'check-key-1') and deletes it at the end.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models.purchase_requisition import PurchaseRequisition

TEMP_KEY = "check-key-1"


def make(**overrides) -> PurchaseRequisition:
    data = dict(
        requester="test.user",
        department="Procurement",
        item_id="I001",
        quantity=5,
        estimated_budget=Decimal("50000.10"),
        required_delivery_date=date(2026, 12, 1),
    )
    data.update(overrides)
    return PurchaseRequisition(**data)


def attempt(label: str, expect_ok: bool, **overrides) -> bool:
    with SessionLocal() as s:  # own session per attempt: a failure cannot affect the next
        try:
            pr = make(**overrides)
            s.add(pr)
            s.commit()
            result = f"accepted ({pr.pr_number}, status={pr.status})"
            passed = expect_ok
        except IntegrityError as exc:
            s.rollback()
            result = f"rejected: {str(exc.orig)[:55]}"
            passed = not expect_ok
    print(f"{'PASS' if passed else 'FAIL'}  {label:26} {result}")
    return passed


def count_rows() -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(PurchaseRequisition))


def main() -> None:
    before = count_rows()
    try:
        results = [
            attempt("valid PR", True, idempotency_key=TEMP_KEY),
            attempt("unknown item (bad FK)", False, item_id="NOPE"),
            attempt("quantity 0", False, quantity=0),
            attempt("negative budget", False, estimated_budget=Decimal("-1")),
            attempt("duplicate idempotency key", False, idempotency_key=TEMP_KEY),
        ]
    finally:
        with SessionLocal() as s:
            s.execute(delete(PurchaseRequisition).where(PurchaseRequisition.idempotency_key == TEMP_KEY))
            s.commit()
    after = count_rows()
    print(f"\nPR rows before={before} after={after}")
    print("ALL PASS" if all(results) and before == after else "SOMETHING FAILED")


if __name__ == "__main__":
    main()
