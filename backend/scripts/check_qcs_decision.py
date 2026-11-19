"""Prove Slice 3f block C over HTTP: QCS decisions (D67-D74).

Run from backend/ (after seed_vendors and seed_items):
    python -m scripts.check_qcs_decision

Creates PRs tagged requester 'check.qcs.3fc' and vendors CHK3C01..03; everything, including audit rows,
is deleted at the end (and at the start, in case an earlier run crashed). Multi-level cases patch
rules.required_qcs_approval_levels (the real placeholder returns 1) and restore it afterwards.
"""
import json
import logging
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

import app.core.rules as rules
import app.services.qcs as svc
from app.core.enums import ApprovalDecision
from app.db.database import SessionLocal
from app.main import app
from app.models import QCS, Approval, AuditEvent, Item, Quotation, RFQ, Vendor
from app.schemas.qcs import QCSDecision
from scripts._helpers import cleanup, total_rows

TAG = "check.qcs.3fc"
BUYER = f"{TAG}.buyer"                       # creates and submits every QCS
A1, A2, A3 = (f"{TAG}.appr{i}" for i in (1, 2, 3))
ATT = "QCS_DUPLICATE_DECISION_ATTEMPT"
client = TestClient(app)
quiet = TestClient(app, raise_server_exceptions=False)   # an unhandled server error shows up as a 500
results: list[bool] = []
TODAY = date.today()
FUTURE = (TODAY + timedelta(days=30)).isoformat()
IN_7 = (TODAY + timedelta(days=7)).isoformat()
V = {i: f"CHK3C{i:02d}" for i in range(1, 4)}
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


def body_of(r) -> dict:
    return r.json() if r.status_code in (200, 201) else {}


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
    pr = client.post("/api/requisitions", json=body, headers={"Idempotency-Key": f"chk-3fc-pr-{_n['pr']}"}).json()["id"]
    assert client.post(f"/api/requisitions/{pr}/decision", json={"approver": "mgr.3fc", "decision": "APPROVED"}).status_code == 201
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


def draft_qcs():
    """A CLOSED RFQ with a DRAFT QCS (V1 pick, lowest, full). Returns (rfq_id, qcs_id, pick_id)."""
    rfq = closed_rfq()
    pick = qid(rfq, 1)
    r = client.post(f"/api/rfq/{rfq}/qcs", json={"created_by": BUYER, "recommended_quotation_id": pick, "justification": J1})
    assert r.status_code == 201, r.text
    return rfq, r.json()["id"], pick


def pending_qcs():
    """Same, but submitted: PENDING_APPROVAL. Returns (rfq_id, qcs_id, pick_id)."""
    rfq, q_, pick = draft_qcs()
    r = client.post(f"/api/qcs/{q_}/submit", json={"actor": BUYER})
    assert r.status_code == 200, r.text
    return rfq, q_, pick


def decide(qcs_id, approver=A1, decision="APPROVED", comments=None, key=None, corr=None, c=client):
    body = {"approver": approver, "decision": decision}
    if comments is not None:
        body["comments"] = comments
    headers = {}
    if key:
        headers["Idempotency-Key"] = key
    if corr:
        headers["X-Correlation-ID"] = corr
    return c.post(f"/api/qcs/{qcs_id}/decision", json=body, headers=headers)


def cancel(rfq_id):
    return client.post(f"/api/rfq/{rfq_id}/cancel", json={"actor": BUYER, "reason": "re-tender"})


def qrow(qcs_id) -> tuple:
    with SessionLocal() as s:
        q = s.get(QCS, qcs_id)
        return (q.status, q.recommended_quotation_id, q.justification, q.updated_at)


def rfq_status(rfq_id) -> str:
    with SessionLocal() as s:
        return s.scalar(select(RFQ.status).where(RFQ.id == rfq_id))


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


def db_set_rfq(rfq_id, **values) -> None:
    with SessionLocal() as s:
        s.execute(update(RFQ).where(RFQ.id == rfq_id).values(**values))
        s.commit()


def db_add_approval(qcs_id, level, approver, decision="APPROVED", key=None, digest=None) -> None:
    with SessionLocal() as s:
        s.add(Approval(qcs_id=qcs_id, level=level, approval_cycle=1, approver=approver, decision=decision,
                       idempotency_key=key, request_hash=digest))
        s.commit()


def set_levels(n: int) -> None:
    rules.required_qcs_approval_levels = lambda _quotation: n


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
        s.execute(delete(AuditEvent).where(AuditEvent.actor.like("CHK3C%")))
        s.execute(delete(Vendor).where(Vendor.vendor_id.like("CHK3C%")))
        s.commit()


def main() -> None:
    purge()
    before_total = total_rows()
    original_audit = svc.record_audit
    original_levels = rules.required_qcs_approval_levels
    original_find = svc._find_decision_by_key
    svc_logger = logging.getLogger("app.services.qcs")
    original_log_level = svc_logger.level
    Session.execute = _patched_execute
    with SessionLocal() as s:
        category = s.scalar(select(Item.category).where(Item.item_id == "I001"))
        for i, vid in V.items():
            s.add(Vendor(vendor_id=vid, vendor_name=f"Check {vid}", category=category, oem_approved=True, is_active=True))
        s.commit()
    try:
        set_levels(1)
        print("--- D67: unknown, wrong state, bad input")
        r = decide(999999)
        check("unknown QCS -> 404 QCS_NOT_FOUND", r.status_code == 404 and code(r) == "QCS_NOT_FOUND")
        rfqD, qD, _p = draft_qcs()
        snap = total_rows()
        r = decide(qD)
        check("a DRAFT QCS -> 409 QCS_NOT_PENDING (+ qcs_status DRAFT)",
              r.status_code == 409 and code(r) == "QCS_NOT_PENDING" and detail_of(r).get("qcs_status") == "DRAFT")
        check("...nothing written, no refused-attempt row (DRAFT is not 'decided')",
              total_rows() == snap and n_audit(qD, ATT) == 0)
        r = decide(qD, approver=BUYER)
        check("the creator on a DRAFT QCS -> 403 first (403 before 409)",
              r.status_code == 403 and code(r) == "SEPARATION_OF_DUTIES" and detail_of(r).get("reason") == "APPROVER_IS_CREATOR")
        url = f"/api/qcs/{qD}/decision"
        check("blank approver -> 422", client.post(url, json={"approver": "  ", "decision": "APPROVED"}).status_code == 422)
        check("RETURNED -> 422 (PR only)", client.post(url, json={"approver": A1, "decision": "RETURNED", "comments": "x"}).status_code == 422)
        check("REJECTED without comments -> 422", client.post(url, json={"approver": A1, "decision": "REJECTED"}).status_code == 422)
        check("a client-chosen level -> 422", client.post(url, json={"approver": A1, "decision": "APPROVED", "level": 2}).status_code == 422)
        check("an invalid Idempotency-Key header -> 422", decide(qD, key="bad key!").status_code == 422)
        check("...none of these wrote anything", total_rows() == snap)

        print("--- D68: separation of duties")
        rfqS, qS, _p = pending_qcs()
        snap, row = total_rows(), qrow(qS)
        r = decide(qS, approver=f"  {BUYER.swapcase()} ")
        check("the creator (case and spaces ignored) -> 403 APPROVER_IS_CREATOR",
              r.status_code == 403 and code(r) == "SEPARATION_OF_DUTIES" and detail_of(r).get("reason") == "APPROVER_IS_CREATOR")
        r = decide(qS, approver=TAG.upper())
        check("the PR requester -> 403 APPROVER_IS_REQUESTER",
              r.status_code == 403 and code(r) == "SEPARATION_OF_DUTIES" and detail_of(r).get("reason") == "APPROVER_IS_REQUESTER")
        check("...nothing written, the QCS is untouched", total_rows() == snap and qrow(qS) == row)
        rfqL = closed_rfq()
        with SessionLocal() as s:   # a legacy QCS with NO creator, as direct SQL could leave it
            legacy = QCS(rfq_id=rfqL, status="PENDING_APPROVAL", recommended_quotation_id=qid(rfqL, 1), justification=J1)
            s.add(legacy)
            s.commit()
            legacy_id = legacy.id
        r = decide(legacy_id, approver=TAG)
        check("a legacy QCS (no creator): the PR requester is still refused -> 403",
              r.status_code == 403 and detail_of(r).get("reason") == "APPROVER_IS_REQUESTER")
        r = decide(legacy_id, approver=A1)
        check("a legacy QCS (no creator): the creator rule is skipped -> 201 APPROVED",
              r.status_code == 201 and body_of(r).get("qcs_status") == "APPROVED", f"status={r.status_code}")

        print("--- D69: one level, approve")
        rfq1, q1, p1 = pending_qcs()
        r = decide(q1, approver=A1, comments="  fine  ", corr="corr-3fc-1")
        j = body_of(r)
        check("201: level 1, cycle 1, APPROVED, the QCS is now APPROVED, 1 level required",
              r.status_code == 201 and j.get("level") == 1 and j.get("approval_cycle") == 1 and j.get("decision") == "APPROVED"
              and j.get("qcs_status") == "APPROVED" and j.get("levels_required") == 1 and j.get("approver") == A1
              and j.get("comments") == "fine" and j.get("qcs_id") == q1, f"status={r.status_code}")
        g = client.get(f"/api/qcs/{q1}").json()
        check("GET: status APPROVED, levels_approved 1", g.get("status") == "APPROVED" and g.get("levels_approved") == 1)
        rows = audit_rows(q1, "QCS_APPROVED")
        before = json.loads(rows[0].before_state) if rows else {}
        after = json.loads(rows[0].after_state) if rows else {}
        check("one QCS_APPROVED row: before PENDING; after APPROVED with level, levels_required, decision, comments",
              len(rows) == 1 and before == {"status": "PENDING_APPROVAL"} and after.get("status") == "APPROVED"
              and after.get("level") == 1 and after.get("levels_required") == 1 and after.get("decision") == "APPROVED"
              and after.get("comments") == "fine")
        check("audit: actor and correlation id", bool(rows) and rows[0].actor == A1 and rows[0].correlation_id == "corr-3fc-1")
        check("the approvals list has the one decision",
              [(a["level"], a["approver"], a["decision"]) for a in client.get(f"/api/qcs/{q1}/approvals").json()] == [(1, A1, "APPROVED")])
        check("approving changes nothing else: the RFQ is still CLOSED", rfq_status(rfq1) == "CLOSED")
        r = cancel(rfq1)
        check("an APPROVED QCS still blocks cancelling its RFQ (3e rule)", r.status_code == 409 and code(r) == "QCS_IN_PROGRESS")

        print("--- D72: deciding an already decided QCS")
        r = decide(q1, approver=A2)
        check("another approver on an APPROVED QCS -> 409 QCS_NOT_PENDING (+ qcs_status)",
              r.status_code == 409 and code(r) == "QCS_NOT_PENDING" and detail_of(r).get("qcs_status") == "APPROVED")
        att = audit_rows(q1, ATT)
        a0 = json.loads(att[0].after_state) if att else {}
        check("...no approval row added; one refused-attempt row with the reason, decision and status",
              approval_rows(q1) == [(1, A1, "APPROVED")] and len(att) == 1 and att[0].actor == A2
              and a0 == {"reason": "QCS_ALREADY_DECIDED", "attempted_decision": "APPROVED", "qcs_status": "APPROVED"})
        r = decide(q1, approver=A1)
        check("the approver who already decided -> 403 APPROVER_ALREADY_DECIDED_A_LEVEL (403 before 409)",
              r.status_code == 403 and detail_of(r).get("reason") == "APPROVER_ALREADY_DECIDED_A_LEVEL")

        print("--- D69: reject")
        rfq2, q2, _p = pending_qcs()
        r = decide(q2, approver=A1, decision="rejected", comments="too expensive")
        j = body_of(r)
        check("REJECTED -> 201, the QCS is REJECTED (final)",
              r.status_code == 201 and j.get("decision") == "REJECTED" and j.get("qcs_status") == "REJECTED"
              and j.get("comments") == "too expensive", f"status={r.status_code}")
        rows = audit_rows(q2, "QCS_REJECTED")
        after = json.loads(rows[0].after_state) if rows else {}
        check("one QCS_REJECTED row with the comments and the new status",
              len(rows) == 1 and after.get("status") == "REJECTED" and after.get("comments") == "too expensive")
        check("a REJECTED QCS does NOT block cancelling its RFQ -> 200 (3e rule)", cancel(rfq2).status_code == 200)
        r = decide(q2, approver=A2)
        check("a decision on a REJECTED QCS -> 409 QCS_NOT_PENDING (REJECTED); one refused-attempt row",
              r.status_code == 409 and code(r) == "QCS_NOT_PENDING" and detail_of(r).get("qcs_status") == "REJECTED"
              and n_audit(q2, ATT) == 1)
        rfq3, q3, _p = pending_qcs()
        db_set_qcs(q3, status="WITHDRAWN")
        r = decide(q3, approver=A1)
        check("a decision on a WITHDRAWN QCS -> 409 QCS_NOT_PENDING (WITHDRAWN); one refused-attempt row",
              r.status_code == 409 and code(r) == "QCS_NOT_PENDING" and detail_of(r).get("qcs_status") == "WITHDRAWN"
              and n_audit(q3, ATT) == 1)
        rfq4, q4, _p = pending_qcs()
        db_set_rfq(rfq4, status="CANCELLED")
        r = decide(q4, approver=A1)
        check("a PENDING QCS whose RFQ is not CLOSED -> 409 RFQ_NOT_CLOSED (+ rfq_status)",
              r.status_code == 409 and code(r) == "RFQ_NOT_CLOSED" and detail_of(r).get("rfq_status") == "CANCELLED")
        check("...no approval row", approval_rows(q4) == [])

        print("--- D69/D70: several levels (the level function is patched to 2, then 3)")
        set_levels(2)
        rfqM, qM, _p = pending_qcs()
        check("GET shows levels_required 2", client.get(f"/api/qcs/{qM}").json().get("levels_required") == 2)
        row = qrow(qM)
        r = decide(qM, approver=A1, comments="level one ok", corr="corr-3fc-2")
        j = body_of(r)
        check("level 1 of 2 -> 201, level 1, the QCS stays PENDING_APPROVAL, 2 levels required",
              r.status_code == 201 and j.get("level") == 1 and j.get("qcs_status") == "PENDING_APPROVAL"
              and j.get("levels_required") == 2, f"status={r.status_code}")
        check("...the QCS row was NOT touched (status and updated_at unchanged)", qrow(qM) == row)
        rows = audit_rows(qM, "QCS_APPROVAL_LEVEL_APPROVED")
        after = json.loads(rows[0].after_state) if rows else {}
        check("audit: QCS_APPROVAL_LEVEL_APPROVED with no before-state and no status transition",
              len(rows) == 1 and rows[0].before_state is None and "status" not in after
              and after.get("qcs_status") == "PENDING_APPROVAL" and after.get("level") == 1
              and after.get("decision") == "APPROVED" and after.get("levels_required") == 2
              and n_audit(qM, "QCS_APPROVED") == 0)
        check("GET: levels_approved 1", client.get(f"/api/qcs/{qM}").json().get("levels_approved") == 1)
        r = decide(qM, approver=A1)
        check("the same approver for level 2 -> 403 APPROVER_ALREADY_DECIDED_A_LEVEL",
              r.status_code == 403 and detail_of(r).get("reason") == "APPROVER_ALREADY_DECIDED_A_LEVEL")
        r = decide(qM, approver=BUYER)
        check("the creator for level 2 -> 403 APPROVER_IS_CREATOR",
              r.status_code == 403 and detail_of(r).get("reason") == "APPROVER_IS_CREATOR")
        before_ts = qrow(qM)[3]
        r = decide(qM, approver=A2, comments="level two ok")
        j = body_of(r)
        check("level 2 of 2 -> 201, level 2, the QCS is APPROVED",
              r.status_code == 201 and j.get("level") == 2 and j.get("qcs_status") == "APPROVED", f"status={r.status_code}")
        check("...the final level DID change the QCS row (APPROVED, updated_at moved)",
              qrow(qM)[0] == "APPROVED" and qrow(qM)[3] > before_ts)
        rows = audit_rows(qM, "QCS_APPROVED")
        after = json.loads(rows[0].after_state) if rows else {}
        check("one QCS_APPROVED row: before PENDING, after APPROVED at level 2",
              len(rows) == 1 and json.loads(rows[0].before_state) == {"status": "PENDING_APPROVAL"}
              and after.get("status") == "APPROVED" and after.get("level") == 2)
        check("approvals list: level 1 by A1, level 2 by A2, in order",
              approval_rows(qM) == [(1, A1, "APPROVED"), (2, A2, "APPROVED")])

        rfqR, qR, _p = pending_qcs()
        decide(qR, approver=A1)
        r = decide(qR, approver=A2, decision="REJECTED", comments="vendor not suitable")
        j = body_of(r)
        check("REJECTED at level 2 of 2 -> 201, the QCS is REJECTED",
              r.status_code == 201 and j.get("level") == 2 and j.get("qcs_status") == "REJECTED", f"status={r.status_code}")
        check("...approvals: level 1 APPROVED, level 2 REJECTED",
              approval_rows(qR) == [(1, A1, "APPROVED"), (2, A2, "REJECTED")])

        set_levels(3)
        rfqT, qT, _p = pending_qcs()
        r = decide(qT, approver=A1, decision="REJECTED", comments="no")
        check("REJECTED at level 1 of 3 -> the QCS is REJECTED at once",
              r.status_code == 201 and body_of(r).get("qcs_status") == "REJECTED", f"status={r.status_code}")
        rfqU, qU, _p = pending_qcs()
        seen = []
        for who in (A1, A2, A3):
            r = decide(qU, approver=who)
            seen.append((r.status_code, body_of(r).get("level"), body_of(r).get("qcs_status")))
        check("three levels: 1 and 2 stay PENDING_APPROVAL, level 3 makes it APPROVED",
              seen == [(201, 1, "PENDING_APPROVAL"), (201, 2, "PENDING_APPROVAL"), (201, 3, "APPROVED")], str(seen))

        print("--- D73: idempotent decisions (replay ownership)")
        set_levels(2)
        rfqK, qK, _p = pending_qcs()
        K = "chk-3fc-key-1"
        r1 = decide(qK, approver=A1, comments="ok", key=K)
        aid = body_of(r1).get("id")
        check("first call with a key -> 201", r1.status_code == 201 and aid is not None, f"status={r1.status_code}")
        check("the other approver finishes the QCS (level 2 -> APPROVED)",
              decide(qK, approver=A2).status_code == 201 and qrow(qK)[0] == "APPROVED")
        r2 = decide(qK, approver=A1.upper(), comments="ok", key=K)
        j = body_of(r2)
        check("replay (name case ignored) AFTER the QCS moved on -> 200, the ORIGINAL approval, CURRENT qcs_status",
              r2.status_code == 200 and j.get("id") == aid and j.get("level") == 1 and j.get("approver") == A1
              and j.get("qcs_status") == "APPROVED" and r2.headers.get("Idempotent-Replay") == "true",
              f"status={r2.status_code} code={code(r2)}")
        check("...nothing new written", len(approval_rows(qK)) == 2 and n_audit(qK, ATT) == 0)
        r3 = decide(qK, approver=A1, comments="a different comment", key=K)
        check("same key, different content -> 409 IDEMPOTENCY_KEY_REUSED", r3.status_code == 409 and code(r3) == "IDEMPOTENCY_KEY_REUSED")
        r4 = decide(qK, approver=A3, comments="ok", key=K)
        check("the same key from ANOTHER approver -> 409 IDEMPOTENCY_KEY_REUSED, and it names nobody",
              r4.status_code == 409 and code(r4) == "IDEMPOTENCY_KEY_REUSED" and A1.lower() not in r4.text.lower(),
              f"status={r4.status_code}")
        check("...no approval row for the other approver, no refused-attempt row",
              len(approval_rows(qK)) == 2 and n_audit(qK, ATT) == 0)
        rfqK2, qK2, _p = pending_qcs()
        r5 = decide(qK2, approver=A1, comments="ok", key=K)
        check("the same key on ANOTHER QCS is a different request -> 201", r5.status_code == 201 and body_of(r5).get("qcs_id") == qK2)

        print("--- D67: a 403 must not hide my own twin (the twin committed between the two checks)")
        rfqW, qW, _p = pending_qcs()
        K3 = "chk-3fc-key-3"
        rw = decide(qW, approver=A1, key=K3)
        aid3 = body_of(rw).get("id")
        calls = {"n": 0}

        def find_blind_once(session, qcs_id, key):   # the FIRST key lookup sees nothing, as if the twin had not committed yet
            calls["n"] += 1
            return None if calls["n"] == 1 else original_find(session, qcs_id, key)

        svc._find_decision_by_key = find_blind_once
        outcome, extra = None, ""
        try:
            with SessionLocal() as s:
                approval, created = svc.decide_qcs(s, qW, QCSDecision(approver=A1, decision="APPROVED"), idempotency_key=K3)
                outcome = (created is False and approval.id == aid3)
        except svc.QCSError as exc:
            outcome, extra = False, f"{exc.code} {exc.extra}"
        finally:
            svc._find_decision_by_key = original_find
        check("own twin behind a 403 path -> the replay, not APPROVER_ALREADY_DECIDED_A_LEVEL", outcome is True, extra)

        print("--- races")
        set_levels(1)
        names = [f"{TAG}.race{i:02d}" for i in range(10)]
        rfqX, qX, _p = pending_qcs()
        out = run_race(10, lambda i, c: decide(qX, approver=names[i], c=c))
        n201 = sum(r.status_code == 201 for r in out)
        refused = [r for r in out if r.status_code == 409]
        others = [r.status_code for r in out if r.status_code not in (201, 409)]
        check("10 simultaneous decisions, 10 approvers: one 201, nine 409", n201 == 1 and len(refused) == 9 and not others,
              f"201s={n201} 409s={len(refused)} others={others}")
        check("...refusals say QCS_NOT_PENDING (APPROVED); one approval row; one QCS_APPROVED row",
              all(code(r) == "QCS_NOT_PENDING" and detail_of(r).get("qcs_status") == "APPROVED" for r in refused)
              and len(approval_rows(qX)) == 1 and n_audit(qX, "QCS_APPROVED") == 1)
        check("...every refused attempt left one refused-attempt row", n_audit(qX, ATT) == len(refused),
              f"rows={n_audit(qX, ATT)} refused={len(refused)}")

        set_levels(2)
        rfqY, qY, _p = pending_qcs()
        out = run_race(10, lambda i, c: decide(qY, approver=names[i], c=c))
        n201 = sum(r.status_code == 201 for r in out)
        refused = [r for r in out if r.status_code == 409]
        others = [r.status_code for r in out if r.status_code not in (201, 409)]
        rows = approval_rows(qY)
        check("10 simultaneous decisions, 2 levels: only 201 or 409, one or two 201s",
              n201 in (1, 2) and n201 + len(refused) == 10 and not others, f"201s={n201} 409s={len(refused)} others={others}")
        check("...refusals say QCS_NOT_PENDING or QCS_CHANGED", all(code(r) in ("QCS_NOT_PENDING", "QCS_CHANGED") for r in refused))
        check("...approval rows == successes, levels 1..n with no gap or duplicate",
              len(rows) == n201 and [x[0] for x in rows] == list(range(1, n201 + 1)), str(rows))
        check("...the status follows the rows (APPROVED after 2, PENDING_APPROVAL after 1)",
              qrow(qY)[0] == ("APPROVED" if n201 == 2 else "PENDING_APPROVAL"))

        print("--- D71: a race is named by the state that actually won")
        set_levels(1)
        rfq, q_, _p = pending_qcs()
        r, fired = run_forced(lambda: decide(q_, approver=A1), lambda: db_set_qcs(q_, status="APPROVED"))
        check("a decision while another decision wins -> 409 QCS_NOT_PENDING (APPROVED)",
              fired and r.status_code == 409 and code(r) == "QCS_NOT_PENDING" and detail_of(r).get("qcs_status") == "APPROVED",
              f"fired={fired} status={r.status_code} code={code(r)}")
        att = audit_rows(q_, ATT)
        check("...no approval row, no QCS_APPROVED row, one refused-attempt row (CONCURRENT_DECISION)",
              approval_rows(q_) == [] and n_audit(q_, "QCS_APPROVED") == 0 and len(att) == 1
              and json.loads(att[0].after_state).get("reason") == "CONCURRENT_DECISION")

        rfq, q_, _p = pending_qcs()
        r, fired = run_forced(lambda: decide(q_, approver=A1), lambda: db_set_rfq(rfq, status="CANCELLED"))
        check("a decision while the RFQ gets cancelled -> 409 RFQ_NOT_CLOSED",
              fired and r.status_code == 409 and code(r) == "RFQ_NOT_CLOSED" and detail_of(r).get("rfq_status") == "CANCELLED",
              f"fired={fired} status={r.status_code} code={code(r)}")
        check("...the QCS is still PENDING_APPROVAL, no approval row, one refused-attempt row",
              qrow(q_)[0] == "PENDING_APPROVAL" and approval_rows(q_) == [] and n_audit(q_, ATT) == 1)

        set_levels(2)
        rfq, q_, _p = pending_qcs()
        r, fired = run_forced(lambda: decide(q_, approver=A2), lambda: db_add_approval(q_, 1, A3))
        check("a decision while someone else takes the same level -> 409 QCS_CHANGED",
              fired and r.status_code == 409 and code(r) == "QCS_CHANGED" and detail_of(r).get("qcs_status") == "PENDING_APPROVAL",
              f"fired={fired} status={r.status_code} code={code(r)}")
        check("...only THEIR approval row exists; the QCS is still PENDING_APPROVAL; one refused-attempt row",
              approval_rows(q_) == [(1, A3, "APPROVED")] and qrow(q_)[0] == "PENDING_APPROVAL" and n_audit(q_, ATT) == 1)

        rfq, q_, _p = pending_qcs()
        K4 = "chk-3fc-key-4"
        digest = svc._decision_hash(QCSDecision(approver=A1, decision="APPROVED"))
        r, fired = run_forced(lambda: decide(q_, approver=A1, key=K4),
                              lambda: db_add_approval(q_, 1, A1, key=K4, digest=digest))
        check("my own twin commits just before me -> 200 replay, not a conflict",
              fired and r.status_code == 200 and r.headers.get("Idempotent-Replay") == "true",
              f"fired={fired} status={r.status_code} code={code(r)}")
        check("...one approval row, no refused-attempt row, no audit row from this request",
              len(approval_rows(q_)) == 1 and n_audit(q_, ATT) == 0 and n_audit(q_, "QCS_APPROVAL_LEVEL_APPROVED") == 0)

        print("--- D14 / D72: audit failures")

        def boom(*_a, **_k):
            raise RuntimeError("audit is down")

        set_levels(1)
        rfq, q_, _p = pending_qcs()
        svc.record_audit = boom
        r = decide(q_, approver=A1, c=quiet)
        svc.record_audit = original_audit
        check("audit fails -> the decision fails (500); the QCS is still PENDING_APPROVAL; no approval row",
              r.status_code == 500 and qrow(q_)[0] == "PENDING_APPROVAL" and approval_rows(q_) == [], f"status={r.status_code}")
        check("...and the same decision works afterwards", decide(q_, approver=A1).status_code == 201)
        svc_logger.setLevel(logging.CRITICAL)   # the swallowed logging failure is expected here; keep the output clean
        svc.record_audit = boom
        r = decide(q_, approver=A2, c=quiet)
        svc.record_audit = original_audit
        svc_logger.setLevel(original_log_level)
        check("the refused-attempt log fails -> the 409 answer still stands (not a 500)",
              r.status_code == 409 and code(r) == "QCS_NOT_PENDING", f"status={r.status_code}")
        check("...and no refused-attempt row was written", n_audit(q_, ATT) == 0)

        print("--- D63: the service does not trust the schema for a blank approver")
        with SessionLocal() as s:
            try:
                svc.decide_qcs(s, q_, QCSDecision.model_construct(approver="  ", decision=ApprovalDecision.APPROVED, comments=None))
                got = None
            except svc.QCSInvalid as exc:
                got = exc.code
        check("a blank approver straight into the service -> NAME_REQUIRED", got == "NAME_REQUIRED", str(got))
    finally:
        Session.execute = original_execute
        hook["fn"] = None
        svc.record_audit = original_audit
        svc._find_decision_by_key = original_find
        rules.required_qcs_approval_levels = original_levels
        svc_logger.setLevel(original_log_level)
        purge()

    after_total = total_rows()
    with SessionLocal() as s:
        leftover = s.scalar(select(func.count()).select_from(Vendor).where(Vendor.vendor_id.like("CHK3C%")))
    print(f"\nrows before={before_total} after={after_total}  leftover test vendors={leftover}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total and leftover == 0 else "SOMETHING FAILED")


if __name__ == "__main__":
    main()