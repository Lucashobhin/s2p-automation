"""Prove Slice 3f block D over HTTP: withdraw an APPROVED QCS (D75-D79).

Run from backend/ (after seed_vendors and seed_items):
    python -m scripts.check_qcs_withdraw

Creates PRs tagged requester 'check.qcs.3fd' and vendors CHK3D01..03; everything, including POs and audit
rows, is deleted at the end (and at the start, in case an earlier run crashed).
"""
import json
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

import app.services.qcs as svc
from app.db.database import SessionLocal
from app.main import app
from app.models import QCS, Approval, AuditEvent, Item, Quotation, RFQ, Vendor
from app.schemas.qcs import QCSWithdraw
from scripts._helpers import cleanup, new_po, total_rows

TAG = "check.qcs.3fd"
BUYER = f"{TAG}.buyer"                       # creates and submits every QCS
A1, A2 = f"{TAG}.appr1", f"{TAG}.appr2"
W = f"{TAG}.withdrawer"
ATT = "QCS_DUPLICATE_DECISION_ATTEMPT"
client = TestClient(app)
quiet = TestClient(app, raise_server_exceptions=False)   # an unhandled server error shows up as a 500
results: list[bool] = []
TODAY = date.today()
FUTURE = (TODAY + timedelta(days=30)).isoformat()
IN_7 = (TODAY + timedelta(days=7)).isoformat()
V = {i: f"CHK3D{i:02d}" for i in range(1, 4)}
_n = {"pr": 0}
QS = [(1, 100, "4000.00"), (2, 100, "4200.00"), (3, 60, "4500.00")]
J1 = "lowest price and full coverage"


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label:68} {detail}")
    return bool(ok)


def code(r):
    d = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    return d.get("code") if isinstance(d, dict) else None


def detail_of(r) -> dict:
    d = r.json().get("detail")
    return d if isinstance(d, dict) else {}


# ---------- one-shot hook: something happens right BEFORE the QCS UPDATE ----------

original_execute = Session.execute
hook = {"fn": None}


def _patched_execute(self, statement, *args, **kwargs):
    fn = hook["fn"]
    if fn is not None and isinstance(statement, Update) and getattr(statement.table, "name", None) == QCS.__tablename__:
        hook["fn"] = None   # one shot: the interference itself may update qcs
        fn()
    return original_execute(self, statement, *args, **kwargs)


def run_forced(request_fn, interfere_fn):
    hook["fn"] = interfere_fn
    try:
        r = request_fn()
        fired = hook["fn"] is None
    finally:
        hook["fn"] = None
    return r, fired


# ---------- fixtures ----------

def published_rfq(quantity=100, vendors=(1, 2)) -> int:
    _n["pr"] += 1
    body = dict(requester=TAG, department="Procurement", item_id="I001", quantity=quantity,
                estimated_budget="50000.10", currency="INR", required_delivery_date=FUTURE)
    pr = client.post("/api/requisitions", json=body, headers={"Idempotency-Key": f"chk-3fd-pr-{_n['pr']}"}).json()["id"]
    assert client.post(f"/api/requisitions/{pr}/decision", json={"approver": "mgr.3fd", "decision": "APPROVED"}).status_code == 201
    rfq = client.post("/api/rfq/generate", json={"pr_id": pr, "actor": BUYER}).json()["id"]
    r = client.post(f"/api/rfq/{rfq}/publish", json={"actor": BUYER, "vendor_ids": [V[i] for i in vendors], "response_deadline": IN_7})
    assert r.status_code == 200, r.text
    return rfq


def quote(rfq_id, vendor_idx, qty=100, price="4200.00") -> None:
    body = {"vendor_id": V[vendor_idx], "quoted_quantity": qty, "unit_price": price, "tax_rate_percent": "18.00",
            "currency": "INR", "lead_time_days": 14}
    r = client.post(f"/api/rfq/{rfq_id}/quotations", json=body)
    assert r.status_code in (200, 201), r.text


def closed_rfq() -> int:
    rfq = published_rfq(100, vendors=sorted({v for v, _q, _p in QS}))
    for v, q, p in QS:
        quote(rfq, v, qty=q, price=p)
    with SessionLocal() as s:
        s.execute(update(RFQ).where(RFQ.id == rfq).values(
            issue_date=TODAY - timedelta(days=10), response_deadline=TODAY - timedelta(days=1)))
        s.commit()
    r = client.post(f"/api/rfq/{rfq}/close", json={"actor": BUYER})
    assert r.status_code == 200, r.text
    return rfq


def qid(rfq_id, vendor_idx) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Quotation.id).where(
            Quotation.rfq_id == rfq_id, Quotation.vendor_id == V[vendor_idx], Quotation.status == "ACTIVE"))


def decide(qcs_id, approver=A1, decision="APPROVED", comments=None):
    body = {"approver": approver, "decision": decision}
    if comments is not None:
        body["comments"] = comments
    return client.post(f"/api/qcs/{qcs_id}/decision", json=body)


def draft_qcs():
    """A CLOSED RFQ with a DRAFT QCS (V1 pick, lowest, full). Returns (rfq_id, qcs_id, pick_id)."""
    rfq = closed_rfq()
    pick = qid(rfq, 1)
    r = client.post(f"/api/rfq/{rfq}/qcs", json={"created_by": BUYER, "recommended_quotation_id": pick, "justification": J1})
    assert r.status_code == 201, r.text
    return rfq, r.json()["id"], pick


def pending_qcs():
    rfq, q_, pick = draft_qcs()
    r = client.post(f"/api/qcs/{q_}/submit", json={"actor": BUYER})
    assert r.status_code == 200, r.text
    return rfq, q_, pick


def approved_qcs():
    """A QCS in APPROVED state (one approval level). Returns (rfq_id, qcs_id, pick_id)."""
    rfq, q_, pick = pending_qcs()
    r = decide(q_, A1)
    assert r.status_code == 201 and r.json()["qcs_status"] == "APPROVED", r.text
    return rfq, q_, pick


def add_po(rfq_id, vendor_idx, status) -> int:
    """Insert a PO straight into the DB on that vendor's ACTIVE quotation of the RFQ (as 3e's checks do)."""
    with SessionLocal() as s:
        pr_id = s.scalar(select(RFQ.pr_id).where(RFQ.id == rfq_id))
        quotation_id = s.scalar(select(Quotation.id).where(
            Quotation.rfq_id == rfq_id, Quotation.vendor_id == V[vendor_idx], Quotation.status == "ACTIVE"))
        po = new_po(pr_id, quotation_id, V[vendor_idx], quantity=10, status=status)
        s.add(po)
        s.commit()
        return po.id


def withdraw(qcs_id, actor=W, reason="wrong vendor chosen", corr=None, c=client):
    return c.post(f"/api/qcs/{qcs_id}/withdraw", json={"actor": actor, "reason": reason},
                  headers={"X-Correlation-ID": corr} if corr else {})


def qrow(qcs_id) -> tuple:
    with SessionLocal() as s:
        q = s.get(QCS, qcs_id)
        return (q.status, q.recommended_quotation_id, q.justification, q.updated_at)


def rfq_status(rfq_id) -> str:
    with SessionLocal() as s:
        return s.scalar(select(RFQ.status).where(RFQ.id == rfq_id))


def pr_of(rfq_id) -> int:
    with SessionLocal() as s:
        return s.scalar(select(RFQ.pr_id).where(RFQ.id == rfq_id))


def approval_rows(qcs_id) -> list:
    with SessionLocal() as s:
        return [(a.level, a.approver, a.decision) for a in s.scalars(
            select(Approval).where(Approval.qcs_id == qcs_id).order_by(Approval.level, Approval.id))]


def audit_rows(qcs_id, action) -> list:
    with SessionLocal() as s:
        return list(s.scalars(select(AuditEvent).where(
            AuditEvent.entity_type == "QCS", AuditEvent.entity_id == str(qcs_id),
            AuditEvent.action == action).order_by(AuditEvent.id)))


def n_audit(qcs_id, action) -> int:
    return len(audit_rows(qcs_id, action))


def db_set_qcs(qcs_id, **values) -> None:
    with SessionLocal() as s:
        s.execute(update(QCS).where(QCS.id == qcs_id).values(**values))
        s.commit()


def run_race(n, fn) -> list:
    out, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker(i):
        c = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        r = fn(i, c)
        with lock:
            out.append(r)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def purge() -> None:
    cleanup(TAG)
    with SessionLocal() as s:
        s.execute(delete(AuditEvent).where(AuditEvent.actor.like("CHK3D%")))
        s.execute(delete(Vendor).where(Vendor.vendor_id.like("CHK3D%")))
        s.commit()


def main() -> None:
    purge()
    before_total = total_rows()
    original_audit = svc.record_audit
    Session.execute = _patched_execute
    with SessionLocal() as s:
        category = s.scalar(select(Item.category).where(Item.item_id == "I001"))
        for i, vid in V.items():
            s.add(Vendor(vendor_id=vid, vendor_name=f"Check {vid}", category=category, oem_approved=True, is_active=True))
        s.commit()
    try:
        print("--- D75: unknown, bad input, wrong state")
        r = withdraw(999999)
        check("unknown QCS -> 404 QCS_NOT_FOUND", r.status_code == 404 and code(r) == "QCS_NOT_FOUND")
        rfqA, qA, pA = approved_qcs()
        url = f"/api/qcs/{qA}/withdraw"
        snap, row = total_rows(), qrow(qA)
        check("blank actor -> 422", client.post(url, json={"actor": "  ", "reason": "x"}).status_code == 422)
        check("reason missing -> 422", client.post(url, json={"actor": W}).status_code == 422)
        check("blank reason -> 422", client.post(url, json={"actor": W, "reason": "   "}).status_code == 422)
        check("reason over 1000 characters -> 422", client.post(url, json={"actor": W, "reason": "x" * 1001}).status_code == 422)
        check("extra field -> 422", client.post(url, json={"actor": W, "reason": "x", "status": "APPROVED"}).status_code == 422)
        check("an invalid X-Correlation-ID header -> 422",
              client.post(url, json={"actor": W, "reason": "x"}, headers={"X-Correlation-ID": "bad id!"}).status_code == 422)
        check("...none of these wrote anything; the QCS is still APPROVED", total_rows() == snap and qrow(qA) == row
              and qrow(qA)[0] == "APPROVED")

        rfqD, qD, _p = draft_qcs()
        rfqP, qP, _p = pending_qcs()
        rfqJ, qJ, _p = pending_qcs()
        assert decide(qJ, A1, "REJECTED", comments="too dear").status_code == 201
        snap = total_rows()
        for label, qq, st in (("DRAFT", qD, "DRAFT"), ("PENDING_APPROVAL", qP, "PENDING_APPROVAL"), ("REJECTED", qJ, "REJECTED")):
            r = withdraw(qq)
            check(f"a {label} QCS -> 409 QCS_NOT_APPROVED (+ qcs_status)",
                  r.status_code == 409 and code(r) == "QCS_NOT_APPROVED" and detail_of(r).get("qcs_status") == st,
                  f"status={r.status_code} code={code(r)}")
        check("...nothing was written, no refused-attempt row", total_rows() == snap and n_audit(qD, ATT) == 0)

        print("--- D77/D79: a successful withdraw keeps the history")
        approvals_before, ts_before = approval_rows(qA), qrow(qA)[3]
        r = withdraw(qA, actor=f"  {BUYER}  ", reason="  chose the wrong vendor  ", corr="corr-3fd-1")
        j = r.json() if r.status_code == 200 else {}
        check("200 WITHDRAWN (the creator may withdraw: no separation rule); the RFQ is still CLOSED",
              r.status_code == 200 and j.get("status") == "WITHDRAWN" and j.get("rfq_status") == "CLOSED"
              and j.get("recommended_quotation_id") == pA, f"status={r.status_code} {detail_of(r)}")
        rows = audit_rows(qA, "QCS_WITHDRAWN")
        before = json.loads(rows[0].before_state) if rows else {}
        after = json.loads(rows[0].after_state) if rows else {}
        check("one QCS_WITHDRAWN row: before APPROVED; after WITHDRAWN with the trimmed reason",
              len(rows) == 1 and before == {"status": "APPROVED"} and after == {"status": "WITHDRAWN", "reason": "chose the wrong vendor"})
        check("audit: cleaned actor and correlation id", bool(rows) and rows[0].actor == BUYER and rows[0].correlation_id == "corr-3fd-1")
        check("updated_at moved (a real state change)", qrow(qA)[3] > ts_before)
        check("approval rows are KEPT, unchanged", approval_rows(qA) == approvals_before == [(1, A1, "APPROVED")])
        check("the approvals endpoint still lists the decision",
              [(a["level"], a["approver"], a["decision"]) for a in client.get(f"/api/qcs/{qA}/approvals").json()] == [(1, A1, "APPROVED")])
        g = client.get(f"/api/qcs/{qA}").json()
        check("GET: status WITHDRAWN, levels_approved still 1, the RFQ is CLOSED",
              g.get("status") == "WITHDRAWN" and g.get("levels_approved") == 1 and rfq_status(rfqA) == "CLOSED")

        print("--- D79: WITHDRAWN is terminal")
        snap = total_rows()
        r = withdraw(qA)
        check("withdraw again -> 409 QCS_NOT_APPROVED (WITHDRAWN)",
              r.status_code == 409 and code(r) == "QCS_NOT_APPROVED" and detail_of(r).get("qcs_status") == "WITHDRAWN")
        r = client.patch(f"/api/qcs/{qA}", json={"actor": BUYER, "justification": "late change"})
        check("edit -> 409 QCS_NOT_DRAFT (WITHDRAWN)",
              r.status_code == 409 and code(r) == "QCS_NOT_DRAFT" and detail_of(r).get("qcs_status") == "WITHDRAWN")
        r = client.post(f"/api/qcs/{qA}/submit", json={"actor": BUYER})
        check("submit -> 409 QCS_NOT_DRAFT (WITHDRAWN)",
              r.status_code == 409 and code(r) == "QCS_NOT_DRAFT" and detail_of(r).get("qcs_status") == "WITHDRAWN")
        check("...those three refusals wrote nothing", total_rows() == snap and n_audit(qA, "QCS_WITHDRAWN") == 1)
        r = decide(qA, A2)
        check("a decision -> 409 QCS_NOT_PENDING (WITHDRAWN) and one refused-attempt row (block C rule)",
              r.status_code == 409 and code(r) == "QCS_NOT_PENDING" and detail_of(r).get("qcs_status") == "WITHDRAWN"
              and n_audit(qA, ATT) == 1)
        r = client.post(f"/api/rfq/{rfqA}/qcs", json={"created_by": BUYER})
        check("a second QCS for the same RFQ -> 409 QCS_EXISTS (WITHDRAWN)",
              r.status_code == 409 and code(r) == "QCS_EXISTS" and detail_of(r).get("qcs_status") == "WITHDRAWN")
        r = client.post(f"/api/rfq/{rfqA}/cancel", json={"actor": BUYER, "reason": "re-tender"})
        check("a WITHDRAWN QCS does NOT block cancelling its RFQ -> 200 CANCELLED",
              r.status_code == 200 and rfq_status(rfqA) == "CANCELLED", f"status={r.status_code} code={code(r)}")
        check("...the QCS stays WITHDRAWN", qrow(qA)[0] == "WITHDRAWN")
        r = client.post("/api/rfq/generate", json={"pr_id": pr_of(rfqA), "actor": BUYER})
        check("...and the PR can be re-tendered: round 2", r.status_code == 201 and r.json().get("round_number") == 2,
              f"status={r.status_code} code={code(r)}")
        r = client.post(f"/api/rfq/{rfqA}/qcs", json={"created_by": BUYER})
        check("a new QCS on the cancelled RFQ -> 409 RFQ_NOT_CLOSED",
              r.status_code == 409 and code(r) == "RFQ_NOT_CLOSED" and detail_of(r).get("rfq_status") == "CANCELLED")

        print("--- D76: a live PO on ANY quotation of the RFQ blocks the withdraw")
        for st, idx in (("PENDING_APPROVAL", 1), ("APPROVED", 1), ("ERP_PUSHED", 1), ("PENDING_APPROVAL", 2), ("APPROVED", 2)):
            rfq, q_, _p = approved_qcs()
            add_po(rfq, idx, st)
            snap = total_rows()
            r = withdraw(q_)
            where = "the recommended quotation" if idx == 1 else "ANOTHER quotation of the RFQ"
            check(f"a {st} PO on {where} -> 409 PO_EXISTS (1 PO)",
                  r.status_code == 409 and code(r) == "PO_EXISTS" and detail_of(r).get("purchase_orders") == 1,
                  f"status={r.status_code} code={code(r)}")
            check("...the QCS is still APPROVED; nothing written, no audit row",
                  qrow(q_)[0] == "APPROVED" and total_rows() == snap and n_audit(q_, "QCS_WITHDRAWN") == 0)
        rfq, q_, _p = approved_qcs()
        add_po(rfq, 1, "PENDING_APPROVAL")
        add_po(rfq, 2, "APPROVED")
        add_po(rfq, 3, "CANCELLED")
        r = withdraw(q_)
        check("two live POs and one CANCELLED -> 409 PO_EXISTS counts 2",
              r.status_code == 409 and code(r) == "PO_EXISTS" and detail_of(r).get("purchase_orders") == 2, f"status={r.status_code}")

        print("--- D76: what does NOT block")
        for st in ("CANCELLED", "REJECTED"):
            rfq, q_, _p = approved_qcs()
            add_po(rfq, 1, st)
            r = withdraw(q_)
            check(f"a {st} PO does NOT block -> 200 WITHDRAWN", r.status_code == 200 and r.json().get("status") == "WITHDRAWN",
                  f"status={r.status_code} code={code(r)}")
        rfqO, qO, _p = approved_qcs()
        add_po(rfqO, 1, "PENDING_APPROVAL")
        rfqW, qW, _p = approved_qcs()
        r = withdraw(qW)
        check("a live PO on ANOTHER RFQ does not block -> 200", r.status_code == 200 and r.json().get("status") == "WITHDRAWN",
              f"status={r.status_code} code={code(r)}")
        check("...the other RFQ's QCS is untouched", qrow(qO)[0] == "APPROVED")

        print("--- races")
        rfqR, qR, _p = approved_qcs()
        out = run_race(10, lambda i, c: withdraw(qR, c=c))
        n200 = sum(r.status_code == 200 for r in out)
        refused = [r for r in out if r.status_code == 409]
        others = [r.status_code for r in out if r.status_code not in (200, 409)]
        check("10 simultaneous withdraws: one 200, nine 409", n200 == 1 and len(refused) == 9 and not others,
              f"200s={n200} 409s={len(refused)} others={others}")
        check("...refusals say QCS_NOT_APPROVED (WITHDRAWN); one audit row; the approval is kept",
              all(code(r) == "QCS_NOT_APPROVED" and detail_of(r).get("qcs_status") == "WITHDRAWN" for r in refused)
              and n_audit(qR, "QCS_WITHDRAWN") == 1 and approval_rows(qR) == [(1, A1, "APPROVED")])

        print("--- D78: a race is named by the state that actually won (the UPDATE is atomic)")
        rfq, q_, _p = approved_qcs()
        r, fired = run_forced(lambda: withdraw(q_), lambda: add_po(rfq, 1, "PENDING_APPROVAL"))
        check("a live PO appears just before the update -> 409 PO_EXISTS (1 PO)",
              fired and r.status_code == 409 and code(r) == "PO_EXISTS" and detail_of(r).get("purchase_orders") == 1,
              f"fired={fired} status={r.status_code} code={code(r)}")
        check("...the QCS is still APPROVED, no audit row", qrow(q_)[0] == "APPROVED" and n_audit(q_, "QCS_WITHDRAWN") == 0)
        rfq, q_, _p = approved_qcs()
        r, fired = run_forced(lambda: withdraw(q_), lambda: add_po(rfq, 1, "CANCELLED"))
        check("a CANCELLED PO appears just before the update -> still 200 (it is not live)",
              fired and r.status_code == 200 and r.json().get("status") == "WITHDRAWN", f"fired={fired} status={r.status_code} code={code(r)}")
        rfq, q_, _p = approved_qcs()
        r, fired = run_forced(lambda: withdraw(q_), lambda: db_set_qcs(q_, status="WITHDRAWN"))
        check("another withdraw wins first -> 409 QCS_NOT_APPROVED (WITHDRAWN)",
              fired and r.status_code == 409 and code(r) == "QCS_NOT_APPROVED" and detail_of(r).get("qcs_status") == "WITHDRAWN",
              f"fired={fired} status={r.status_code} code={code(r)}")
        check("...this request wrote no audit row", n_audit(q_, "QCS_WITHDRAWN") == 0)

        print("--- D14: audit failure rolls the withdraw back")

        def boom(*_a, **_k):
            raise RuntimeError("audit is down")

        rfq, q_, _p = approved_qcs()
        svc.record_audit = boom
        r = withdraw(q_, c=quiet)
        svc.record_audit = original_audit
        check("audit fails -> withdraw fails (500); the QCS is still APPROVED; no audit row",
              r.status_code == 500 and qrow(q_)[0] == "APPROVED" and n_audit(q_, "QCS_WITHDRAWN") == 0, f"status={r.status_code}")
        check("...and the same withdraw works afterwards", withdraw(q_).status_code == 200)

        print("--- D75: the service does not trust the schema")
        rfq, q_, _p = approved_qcs()
        with SessionLocal() as s:
            got = []
            for body in (QCSWithdraw.model_construct(actor="  ", reason="x"),
                         QCSWithdraw.model_construct(actor=W, reason="   "),
                         QCSWithdraw.model_construct(actor=W, reason="x" * 1001)):
                try:
                    svc.withdraw_qcs(s, q_, body)
                    got.append(None)
                except svc.QCSInvalid as exc:
                    got.append(exc.code)
        check("blank actor / blank reason / reason over 1000 straight into the service",
              got == ["NAME_REQUIRED", "REASON_REQUIRED", "REASON_TOO_LONG"], str(got))
        check("...nothing was written; the QCS is still APPROVED", qrow(q_)[0] == "APPROVED" and n_audit(q_, "QCS_WITHDRAWN") == 0)
        with SessionLocal() as s:
            svc.withdraw_qcs(s, q_, QCSWithdraw.model_construct(actor=W, reason="  not trimmed  "))
        rows = audit_rows(q_, "QCS_WITHDRAWN")
        check("an untrimmed reason straight into the service is stored trimmed",
              len(rows) == 1 and json.loads(rows[0].after_state).get("reason") == "not trimmed")
    finally:
        Session.execute = original_execute
        hook["fn"] = None
        svc.record_audit = original_audit
        purge()

    after_total = total_rows()
    with SessionLocal() as s:
        leftover = s.scalar(select(func.count()).select_from(Vendor).where(Vendor.vendor_id.like("CHK3D%")))
    print(f"\nrows before={before_total} after={after_total}  leftover test vendors={leftover}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total and leftover == 0 else "SOMETHING FAILED")


if __name__ == "__main__":
    main()