"""Prove the Approval and Audit Event rules (D9-D14, option A).

Run from backend/ (after seed_vendors and seed_items):
    python -m scripts.check_approvals_audit

Creates temporary data tagged 'check.appr.2c3' and deletes it at the end.
"""
import json

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models import (
    QCS, Approval, AuditEvent, PurchaseOrder, PurchaseRequisition, RFQ, RFQVendor,
)
from app.services.audit import record_audit
from scripts._helpers import cleanup, new_po, new_pr, new_quote, total_rows

TAG = "check.appr.2c3"
results: list[bool] = []


def attempt(label, build, expect_error=None) -> None:
    """expect_error=None: must succeed. Otherwise must raise IntegrityError containing that text."""
    with SessionLocal() as s:
        try:
            build(s)
            s.commit()
            outcome, passed = "accepted", expect_error is None
        except IntegrityError as exc:
            s.rollback()
            msg = str(exc.orig)
            outcome, passed = f"rejected: {msg[:60]}", expect_error is not None and expect_error in msg
        except Exception as exc:
            s.rollback()
            outcome, passed = f"UNEXPECTED {type(exc).__name__}: {exc}"[:90], False
    report(label, passed, outcome)


def report(label, passed, detail="") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {label:50} {detail}")


def appr(**kw) -> Approval:
    data = dict(level=1, approval_cycle=1, approver="mgr.test", decision="APPROVED")
    data.update(kw)
    return Approval(**data)


def main() -> None:
    before = total_rows()
    try:
        with SessionLocal() as s:                       # setup: PRs A, B, C, a QCS and a PO
            a, b, c = new_pr(s, TAG), new_pr(s, TAG), new_pr(s, TAG)
            rfq = RFQ(pr_id=a.id, round_number=1)
            s.add(rfq)
            s.flush()
            s.add(RFQVendor(rfq_id=rfq.id, vendor_id="V001"))
            s.flush()
            q = new_quote(rfq.id, "V001")
            s.add(q)
            s.flush()
            qcs = QCS(rfq_id=rfq.id, recommended_quotation_id=q.id)
            po = new_po(a.id, q.id, "V001")
            s.add_all([qcs, po])
            s.commit()
            A, B, C, QCS1, PO1 = a.id, b.id, c.id, qcs.id, po.id
        print("setup ok: 3 PRs, 1 QCS, 1 PO\n")

        print("--- Approvals: one decision per level per cycle (option A)")
        attempt("PR level 1, cycle 1", lambda s: s.add(appr(pr_id=A)))
        attempt("2nd decision, same PR/level/cycle", lambda s: s.add(appr(pr_id=A, decision="REJECTED")), "UNIQUE")
        attempt("PR level 2, cycle 1", lambda s: s.add(appr(pr_id=A, level=2)))
        attempt("PR level 1, cycle 2 (new cycle)", lambda s: s.add(appr(pr_id=A, approval_cycle=2)))
        attempt("PR RETURNED is allowed", lambda s: s.add(appr(pr_id=B, decision="RETURNED")))
        attempt("QCS level 1, cycle 1 (same numbers)", lambda s: s.add(appr(qcs_id=QCS1)))
        attempt("QCS duplicate level/cycle", lambda s: s.add(appr(qcs_id=QCS1)), "UNIQUE")
        attempt("PO level 1, cycle 1", lambda s: s.add(appr(po_id=PO1)))
        attempt("PO duplicate level/cycle", lambda s: s.add(appr(po_id=PO1)), "UNIQUE")

        print("\n--- Approvals: shape rules")
        attempt("no entity at all", lambda s: s.add(appr()), "ck_approval_exactly_one_entity")
        attempt("two entities at once", lambda s: s.add(appr(pr_id=A, qcs_id=QCS1, approval_cycle=9)), "ck_approval_exactly_one_entity")
        attempt("RETURNED on a QCS", lambda s: s.add(appr(qcs_id=QCS1, level=2, decision="RETURNED")), "ck_approval_returned_only_pr")
        attempt("unknown decision value", lambda s: s.add(appr(pr_id=B, level=2, decision="MAYBE")), "ck_approval_decision_valid")
        attempt("level 0", lambda s: s.add(appr(pr_id=B, level=0)), "ck_approval_level_positive")
        attempt("cycle 0", lambda s: s.add(appr(pr_id=B, approval_cycle=0)), "ck_approval_cycle_positive")
        attempt("approval for unknown PR", lambda s: s.add(appr(pr_id=999999)), "FOREIGN KEY")
        attempt("idempotency key first use", lambda s: s.add(appr(pr_id=C, idempotency_key="chk-ap-1")))
        attempt("same key again (retry)", lambda s: s.add(appr(pr_id=C, level=2, idempotency_key="chk-ap-1")), "UNIQUE")

        print("\n--- RESTRICT: approval history protects its record")
        attempt("delete a PR that has approval history", lambda s: s.execute(delete(PurchaseRequisition).where(PurchaseRequisition.id == C)), "FOREIGN KEY")

        print("\n--- Audit events (D12, D13, D14)")
        with SessionLocal() as s:
            record_audit(s, actor=TAG, action="PR_APPROVED", entity_type="PR", entity_id=A,
                         before={"status": "SUBMITTED"}, after={"status": "APPROVED"}, correlation_id="wf-chk-1")
            record_audit(s, actor=TAG, action="PR_RFQ_CREATED", entity_type="PR", entity_id=A, correlation_id="wf-chk-1")
            s.commit()
        with SessionLocal() as s:
            rows = list(s.scalars(select(AuditEvent).where(AuditEvent.correlation_id == "wf-chk-1").order_by(AuditEvent.id)))
            ok = (len(rows) == 2 and rows[0].entity_id == str(A)
                  and json.loads(rows[0].before_state) == {"status": "SUBMITTED"}
                  and json.loads(rows[0].after_state) == {"status": "APPROVED"}
                  and rows[1].before_state is None)
        report("audit rows saved, JSON snapshots, lookup by correlation id", ok, f"rows={len(rows)}")

        for label, kw in (("audit without actor", dict(actor="")), ("audit without action", dict(action=" ")),
                          ("audit without entity id", dict(entity_id=None))):
            args = dict(actor=TAG, action="X", entity_type="PR", entity_id=A)
            args.update(kw)
            try:
                with SessionLocal() as s:
                    record_audit(s, **args)
                report(label, False, "no error")
            except ValueError as e:
                report(label, True, f"ValueError: {e}")

        with SessionLocal() as s:                       # D14: state change + FAILING audit -> rolled back
            s.execute(update(PurchaseRequisition).where(PurchaseRequisition.id == B).values(status="APPROVED"))
            try:
                record_audit(s, actor="", action="PR_APPROVED", entity_type="PR", entity_id=B)
                s.commit()
            except ValueError:
                s.rollback()
        with SessionLocal() as s:
            status = s.scalar(select(PurchaseRequisition.status).where(PurchaseRequisition.id == B))
        report("failed audit rolls the state change back", status == "SUBMITTED", f"status={status}")

        with SessionLocal() as s:                       # D14: state change + audit commit together
            s.execute(update(PurchaseRequisition).where(PurchaseRequisition.id == B).values(status="APPROVED"))
            record_audit(s, actor=TAG, action="PR_APPROVED", entity_type="PR", entity_id=B, correlation_id="wf-chk-2")
            s.commit()
        with SessionLocal() as s:
            status = s.scalar(select(PurchaseRequisition.status).where(PurchaseRequisition.id == B))
            n = s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.correlation_id == "wf-chk-2"))
        report("state change and audit row commit together", status == "APPROVED" and n == 1, f"status={status} audit_rows={n}")

        with SessionLocal() as s:                       # D12: audit outlives the deleted record
            d = new_pr(s, TAG)
            record_audit(s, actor=TAG, action="PR_CREATED", entity_type="PR", entity_id=d.id, correlation_id="wf-chk-3")
            s.commit()
            d_id = d.id
        with SessionLocal() as s:
            s.execute(delete(PurchaseRequisition).where(PurchaseRequisition.id == d_id))
            s.commit()
        with SessionLocal() as s:
            n = s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.correlation_id == "wf-chk-3"))
        report("audit row survives deletion of its PR", n == 1, f"audit_rows={n}")
    finally:
        cleanup(TAG)

    after = total_rows()
    print(f"\nrows before={before} after={after}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before == after else "SOMETHING FAILED")


if __name__ == "__main__":
    main()