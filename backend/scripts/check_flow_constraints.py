"""Prove the database itself enforces the RFQ -> Quotation -> QCS -> PO table rules.

Run from backend/ (after seed_vendors and seed_items):
    python -m scripts.check_flow_constraints

Creates temporary data tagged requester='check.flow.2c2' and deletes it at the end.
Each negative test also checks WHICH guard fired (FOREIGN KEY / UNIQUE / CHECK), so a
test cannot pass because of the wrong rule.
NOTE: these POs are inserted directly, so they do NOT touch the over-ordering counter.
That guard is tested separately by scripts/check_po_quantity.py.
"""
from datetime import date
from decimal import Decimal as D

from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models import QCS, PurchaseOrder, Quotation, RFQ, RFQVendor
from scripts._helpers import cleanup, new_po, new_pr, new_quote, new_rfq, total_rows

TAG = "check.flow.2c2"
V_INVITED_1, V_INVITED_2, V_NOT_INVITED = "V001", "V002", "V003"


def attempt(label, build, expect_error=None) -> bool:
    """build(session) adds objects. expect_error=None means it must succeed;
    otherwise it must raise IntegrityError containing that text."""
    with SessionLocal() as s:
        try:
            build(s)
            s.commit()
            outcome, passed = "accepted", expect_error is None
        except IntegrityError as exc:
            s.rollback()
            msg = str(exc.orig)
            outcome = f"rejected: {msg[:62]}"
            passed = expect_error is not None and expect_error in msg
        except Exception as exc:  # unexpected error type = a real bug in the test or models
            s.rollback()
            outcome, passed = f"UNEXPECTED {type(exc).__name__}: {exc}"[:90], False
    print(f"{'PASS' if passed else 'FAIL'}  {label:44} {outcome}")
    return passed


def main() -> None:
    before = total_rows()
    results = []
    try:
        # ---- setup: PR A with RFQ rounds 1 and 2, PR B with one RFQ ----
        with SessionLocal() as s:
            pr_a, pr_b = new_pr(s, TAG), new_pr(s, TAG)
            r1 = new_rfq(pr_id=pr_a.id, round_number=1)
            r2 = new_rfq(pr_id=pr_a.id, round_number=2)
            rb = new_rfq(pr_id=pr_b.id, round_number=1)
            s.add_all([r1, r2, rb])
            s.flush()
            s.add_all([
                RFQVendor(rfq_id=r1.id, vendor_id=V_INVITED_1),
                RFQVendor(rfq_id=r1.id, vendor_id=V_INVITED_2),
                RFQVendor(rfq_id=r2.id, vendor_id=V_INVITED_1),
                RFQVendor(rfq_id=rb.id, vendor_id=V_INVITED_1),
            ])
            s.flush()
            q1 = new_quote(r1.id, V_INVITED_1)   # round 1, vendor 1
            q2 = new_quote(r2.id, V_INVITED_1)   # round 2, vendor 1 (same PR A)
            q3 = new_quote(rb.id, V_INVITED_1)   # PR B
            s.add_all([q1, q2, q3])
            s.commit()
            A, B = pr_a.id, pr_b.id
            R1, R2, RB = r1.id, r2.id, rb.id
            Q1, Q2, Q3 = q1.id, q2.id, q3.id
        print("setup ok: 2 PRs, 3 RFQs, 3 quotations\n")

        P = results.append
        print("--- RFQ rounds and idempotency (D1)")
        P(attempt("duplicate round for same PR", lambda s: s.add(new_rfq(pr_id=A, round_number=1)), "UNIQUE"))
        P(attempt("round 3 for same PR is allowed", lambda s: s.add(new_rfq(pr_id=A, round_number=3))))
        P(attempt("round 0 rejected", lambda s: s.add(new_rfq(pr_id=A, round_number=0)), "CHECK"))
        P(attempt("RFQ for unknown PR", lambda s: s.add(new_rfq(pr_id=999999, round_number=1)), "FOREIGN KEY"))
        P(attempt("deadline before issue date", lambda s: s.add(new_rfq(pr_id=B, round_number=2, issue_date=date(2026, 12, 10), response_deadline=date(2026, 12, 1))), "CHECK"))
        P(attempt("idempotency key first use", lambda s: s.add(new_rfq(pr_id=B, round_number=5, idempotency_key="chk-rfq-1"))))
        P(attempt("same key again (retry) rejected", lambda s: s.add(new_rfq(pr_id=B, round_number=6, idempotency_key="chk-rfq-1")), "UNIQUE"))

        print("\n--- Invitations and quotations (D2, D3, D4)")
        P(attempt("same vendor invited twice", lambda s: s.add(RFQVendor(rfq_id=R1, vendor_id=V_INVITED_1)), "UNIQUE"))
        P(attempt("invite unknown vendor", lambda s: s.add(RFQVendor(rfq_id=R1, vendor_id="NOPE")), "FOREIGN KEY"))
        P(attempt("UNINVITED vendor cannot quote", lambda s: s.add(new_quote(R1, V_NOT_INVITED)), "FOREIGN KEY"))
        P(attempt("second quotation, same vendor+RFQ", lambda s: s.add(new_quote(R1, V_INVITED_1)), "UNIQUE"))
        P(attempt("quoted quantity 0", lambda s: s.add(new_quote(R1, V_INVITED_2, qty=1, quoted_quantity=0)), "CHECK"))
        P(attempt("negative unit price", lambda s: s.add(Quotation(rfq_id=R1, vendor_id=V_INVITED_2, quoted_quantity=1, unit_price=D("-1"), tax_rate_percent=D("18"), net_amount=D("0"), tax_amount=D("0"), total_amount=D("0"), lead_time_days=1)), "CHECK"))
        P(attempt("tax rate 150%", lambda s: s.add(new_quote(R1, V_INVITED_2, tax_rate_percent=D("150"))), "CHECK"))
        P(attempt("negative lead time", lambda s: s.add(new_quote(R1, V_INVITED_2, lead_time_days=-1)), "CHECK"))

        print("\n--- QCS (D5)")
        P(attempt("QCS recommends quote from OTHER RFQ", lambda s: s.add(QCS(rfq_id=R1, recommended_quotation_id=Q2)), "FOREIGN KEY"))
        P(attempt("QCS with no recommendation yet", lambda s: s.add(QCS(rfq_id=R2))))
        P(attempt("second QCS for same RFQ", lambda s: s.add(QCS(rfq_id=R2)), "UNIQUE"))
        P(attempt("QCS for unknown RFQ", lambda s: s.add(QCS(rfq_id=999999)), "FOREIGN KEY"))

        print("\n--- Purchase orders (D6, B-lite)")
        P(attempt("valid PO from quote 1", lambda s: s.add(new_po(A, Q1, V_INVITED_1))))
        P(attempt("second PO from SAME quotation", lambda s: s.add(new_po(B, Q1, V_INVITED_1)), "UNIQUE"))
        P(attempt("2nd PO, SAME PR, other quote (B-lite)", lambda s: s.add(new_po(A, Q2, V_INVITED_1))))
        P(attempt("PO vendor differs from quote vendor", lambda s: s.add(new_po(B, Q3, V_INVITED_2)), "FOREIGN KEY"))
        P(attempt("PO quantity 0", lambda s: s.add(new_po(B, Q3, V_INVITED_1, quantity=0)), "CHECK"))
        P(attempt("PO negative amount", lambda s: s.add(new_po(B, Q3, V_INVITED_1, agreed_amount=D("-5"))), "CHECK"))
        P(attempt("valid PO from quote 3 (PR B)", lambda s: s.add(new_po(B, Q3, V_INVITED_1))))
        P(attempt("ERP reference first use", lambda s: s.execute(PurchaseOrder.__table__.update().where(PurchaseOrder.quotation_id == Q1).values(erp_reference="4500000001"))))
        P(attempt("same ERP reference on another PO", lambda s: s.execute(PurchaseOrder.__table__.update().where(PurchaseOrder.quotation_id == Q2).values(erp_reference="4500000001")), "UNIQUE"))
    finally:
        cleanup(TAG)

    after = total_rows()
    print(f"\nrows before={before} after={after}")
    ok = all(results) and before == after
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if ok else "SOMETHING FAILED")


if __name__ == "__main__":
    main()
