"""Prove Slice 3f block B over HTTP: edit a DRAFT QCS (D51, D64) and submit it (D52, D65), with the creator
rule (D60, D63) and race outcomes named from the REAL state (D66).

Run from backend/ (after seed_vendors and seed_items):
    python -m scripts.check_qcs_edit_submit

Creates PRs tagged requester 'check.qcs.3fb' and vendors CHK3B01..03; everything, including audit rows,
is deleted at the end (and at the start, in case an earlier run crashed).
"""
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

import app.services.qcs as svc
from app.db.database import SessionLocal
from app.main import app
from app.models import QCS, AuditEvent, Item, Quotation, RFQ, Vendor
from app.schemas.qcs import QCSCreate, QCSSubmit, QCSUpdate
from scripts._helpers import cleanup, total_rows

TAG = "check.qcs.3fb"
BUYER = f"{TAG}.buyer"
OTHER = f"{TAG}.other"
client = TestClient(app)
quiet = TestClient(app, raise_server_exceptions=False)   # an unhandled server error shows up as a 500
results: list[bool] = []
TODAY = date.today()
FUTURE = (TODAY + timedelta(days=30)).isoformat()
IN_7 = (TODAY + timedelta(days=7)).isoformat()
V = {i: f"CHK3B{i:02d}" for i in range(1, 4)}
_n = {"pr": 0}
# unit price incl. tax: V1 4720.0000 (full), V2 4956.0000 (full), V3 5310.0000 (60 of 100)
QS = [(1, 100, "4000.00"), (2, 100, "4200.00"), (3, 60, "4500.00")]
PARTIAL_LOWEST = [(1, 100, "4000.00"), (2, 60, "3000.00")]   # V2: 3540.0000, lowest but partial
J1 = "lowest price and full coverage"            # 30 characters
J2 = "second wording of the same reason"


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label:68} {detail}")
    return ok


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
    """Run the request with `interfere_fn` firing just before its QCS UPDATE. Returns (response, fired)."""
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
    pr = client.post("/api/requisitions", json=body, headers={"Idempotency-Key": f"chk-3fb-pr-{_n['pr']}"}).json()["id"]
    assert client.post(f"/api/requisitions/{pr}/decision", json={"approver": "mgr.3fb", "decision": "APPROVED"}).status_code == 201
    rfq = client.post("/api/rfq/generate", json={"pr_id": pr, "actor": BUYER}).json()["id"]
    r = client.post(f"/api/rfq/{rfq}/publish", json={"actor": BUYER, "vendor_ids": [V[i] for i in vendors], "response_deadline": IN_7})
    assert r.status_code == 200, r.text
    return rfq


def quote(rfq_id, vendor_idx, qty=100, price="4200.00") -> None:
    body = {"vendor_id": V[vendor_idx], "quoted_quantity": qty, "unit_price": price, "tax_rate_percent": "18.00",
            "currency": "INR", "lead_time_days": 14}
    r = client.post(f"/api/rfq/{rfq_id}/quotations", json=body)
    assert r.status_code in (200, 201), r.text


def closed_rfq(quotes=QS) -> int:
    rfq = published_rfq(100, vendors=sorted({v for v, _q, _p in quotes}))
    for v, q, p in quotes:
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


def create(rfq_id, pick=None, just=None, created_by=BUYER):
    body = {"created_by": created_by}
    if pick is not None:
        body["recommended_quotation_id"] = pick
    if just is not None:
        body["justification"] = just
    return client.post(f"/api/rfq/{rfq_id}/qcs", json=body)


def new_draft(rfq_id, pick=None, just=None) -> int:
    r = create(rfq_id, pick=pick, just=just)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def fresh_draft(vendor_idx=1, just=J1):
    """A CLOSED RFQ (QS quotes) with a DRAFT QCS. Returns (rfq_id, qcs_id, pick_id)."""
    rfq = closed_rfq()
    pick = qid(rfq, vendor_idx)
    return rfq, new_draft(rfq, pick=pick, just=just), pick


def patch(qcs_id, body, corr=None, c=client):
    return c.patch(f"/api/qcs/{qcs_id}", json=body, headers={"X-Correlation-ID": corr} if corr else {})


def submit(qcs_id, actor=BUYER, corr=None, c=client):
    return c.post(f"/api/qcs/{qcs_id}/submit", json={"actor": actor}, headers={"X-Correlation-ID": corr} if corr else {})


def qrow(qcs_id) -> tuple:
    with SessionLocal() as s:
        q = s.get(QCS, qcs_id)
        return (q.status, q.recommended_quotation_id, q.justification, q.updated_at)


def n_qcs(rfq_id) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(QCS).where(QCS.rfq_id == rfq_id))


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


def db_set_quote(quotation_id, **values) -> None:
    with SessionLocal() as s:
        s.execute(update(Quotation).where(Quotation.id == quotation_id).values(**values))
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


def length_case(label, rfq, vendor_idx, min_len, reasons) -> None:
    pick = qid(rfq, vendor_idx)
    q_ = new_draft(rfq, pick=pick, just="x" * (min_len - 1))
    r = submit(q_)
    d = detail_of(r)
    check(f"{label}: {min_len - 1} characters -> 422 JUSTIFICATION_TOO_SHORT (min {min_len})",
          r.status_code == 422 and code(r) == "JUSTIFICATION_TOO_SHORT" and d.get("min_length") == min_len
          and d.get("current_length") == min_len - 1 and d.get("reasons") == reasons, f"status={r.status_code} {d}")
    check(f"{label}: ...still DRAFT, no audit row", qrow(q_)[0] == "DRAFT" and n_audit(q_, "QCS_SUBMITTED") == 0)
    check(f"{label}: edit to exactly {min_len} characters is accepted",
          patch(q_, {"actor": BUYER, "justification": "x" * min_len}).status_code == 200)
    r = submit(q_)
    check(f"{label}: exactly {min_len} characters -> 200 PENDING_APPROVAL",
          r.status_code == 200 and r.json().get("status") == "PENDING_APPROVAL", f"status={r.status_code}")


def purge() -> None:
    cleanup(TAG)
    with SessionLocal() as s:
        s.execute(delete(AuditEvent).where(AuditEvent.actor.like("CHK3B%")))
        s.execute(delete(Vendor).where(Vendor.vendor_id.like("CHK3B%")))
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
        print("--- D64: edit a DRAFT QCS (only the fields SENT change)")
        rfqA = closed_rfq()
        pA1, pA2 = qid(rfqA, 1), qid(rfqA, 2)
        qa = new_draft(rfqA, pick=pA1)
        created_at = qrow(qa)[3]
        r = patch(999999, {"actor": BUYER, "justification": "x"})
        check("unknown QCS -> 404 QCS_NOT_FOUND", r.status_code == 404 and code(r) == "QCS_NOT_FOUND")
        snap, row = total_rows(), qrow(qa)
        r = patch(qa, {"actor": OTHER, "justification": "hijack"})
        check("a different name -> 403 NOT_THE_CREATOR", r.status_code == 403 and code(r) == "NOT_THE_CREATOR")
        check("...nothing was written", total_rows() == snap and qrow(qa) == row)
        check("blank actor -> 422", patch(qa, {"actor": "   ", "justification": "x"}).status_code == 422)
        check("extra field (status) -> 422", patch(qa, {"actor": BUYER, "status": "APPROVED"}).status_code == 422)
        r = patch(qa, {"actor": BUYER})
        check("nothing to change -> 422 NOTHING_TO_UPDATE", r.status_code == 422 and code(r) == "NOTHING_TO_UPDATE")
        check("...nothing was written", total_rows() == snap and qrow(qa) == row)

        r = patch(qa, {"actor": f"  {BUYER.swapcase()}  ", "justification": J1}, corr="corr-3fb-1")
        j = r.json() if r.status_code == 200 else {}
        check("creator name ignores case and spaces; justification set, pick untouched",
              r.status_code == 200 and j.get("justification") == J1 and j.get("recommended_quotation_id") == pA1,
              f"status={r.status_code}")
        rows = audit_rows(qa, "QCS_UPDATED")
        after1 = svc.json.loads(rows[-1].after_state) if rows else {}
        before1 = svc.json.loads(rows[-1].before_state) if rows else {}
        check("one QCS_UPDATED row: only the changed field in before and after",
              len(rows) == 1 and before1 == {"justification": None} and after1 == {"justification": J1})
        check("audit: cleaned actor and correlation id", bool(rows) and rows[-1].actor == BUYER.swapcase()
              and rows[-1].correlation_id == "corr-3fb-1")
        check("updated_at moved", qrow(qa)[3] > created_at)

        row = qrow(qa)
        check("the same justification again -> 200 no-op", patch(qa, {"actor": BUYER, "justification": J1}).status_code == 200)
        check("the same pick again -> 200 no-op", patch(qa, {"actor": BUYER, "recommended_quotation_id": pA1}).status_code == 200)
        check("...no write, no new audit row, updated_at unchanged", qrow(qa) == row and n_audit(qa, "QCS_UPDATED") == 1)

        r = patch(qa, {"actor": BUYER, "justification": None})
        j = r.json() if r.status_code == 200 else {}
        check("explicit null clears the justification; the pick stays",
              r.status_code == 200 and j.get("justification") is None and j.get("recommended_quotation_id") == pA1)
        last = audit_rows(qa, "QCS_UPDATED")[-1]
        check("audit: before has the text, after has null, no other field",
              svc.json.loads(last.before_state) == {"justification": J1} and svc.json.loads(last.after_state) == {"justification": None})

        r = patch(qa, {"actor": BUYER, "recommended_quotation_id": None})
        j = r.json() if r.status_code == 200 else {}
        check("explicit null clears the pick: no recommendation, no minimum, no levels",
              r.status_code == 200 and j.get("recommendation") is None and j.get("justification_min_length") is None
              and j.get("levels_required") is None)
        r = patch(qa, {"actor": BUYER, "recommended_quotation_id": pA2})
        j = r.json() if r.status_code == 200 else {}
        check("a new pick (V2, not the lowest): minimum justification becomes 80",
              r.status_code == 200 and j.get("justification_min_length") == 80
              and j["recommendation"]["is_lowest_unit_price_incl_tax"] is False)
        r = patch(qa, {"actor": BUYER, "recommended_quotation_id": pA1, "justification": J2})
        j = r.json() if r.status_code == 200 else {}
        last = audit_rows(qa, "QCS_UPDATED")[-1]
        check("both fields in one PATCH: both change, both in the audit row",
              r.status_code == 200 and j.get("justification") == J2 and j.get("recommended_quotation_id") == pA1
              and svc.json.loads(last.before_state) == {"recommended_quotation_id": pA2, "justification": None}
              and svc.json.loads(last.after_state) == {"recommended_quotation_id": pA1, "justification": J2})
        check("five real changes -> five QCS_UPDATED rows", n_audit(qa, "QCS_UPDATED") == 5)

        rfqB = closed_rfq()
        snap, row = total_rows(), qrow(qa)
        r = patch(qa, {"actor": BUYER, "recommended_quotation_id": qid(rfqB, 1)})
        check("a quotation of ANOTHER RFQ -> 422 QUOTATION_NOT_IN_RFQ", r.status_code == 422 and code(r) == "QUOTATION_NOT_IN_RFQ")
        r = patch(qa, {"actor": BUYER, "recommended_quotation_id": 999999})
        check("a quotation that does not exist -> 422 QUOTATION_NOT_IN_RFQ", r.status_code == 422 and code(r) == "QUOTATION_NOT_IN_RFQ")
        check("quotation id as text -> 422 (strict)", patch(qa, {"actor": BUYER, "recommended_quotation_id": "5"}).status_code == 422)
        check("justification over 1000 characters -> 422", patch(qa, {"actor": BUYER, "justification": "x" * 1001}).status_code == 422)
        check("...none of these wrote anything", total_rows() == snap and qrow(qa) == row)

        print("--- D63: state guards and the ORDER of the checks")
        rfqG, qg, pG = fresh_draft()
        for st in ("PENDING_APPROVAL", "APPROVED", "REJECTED", "WITHDRAWN"):
            db_set_qcs(qg, status=st)
            r1, r2 = patch(qg, {"actor": BUYER, "justification": "new text"}), submit(qg)
            check(f"edit and submit of a {st} QCS -> 409 QCS_NOT_DRAFT (+ qcs_status)",
                  r1.status_code == 409 and code(r1) == "QCS_NOT_DRAFT" and detail_of(r1).get("qcs_status") == st
                  and r2.status_code == 409 and code(r2) == "QCS_NOT_DRAFT" and detail_of(r2).get("qcs_status") == st)
        check("...the QCS was not touched by the refusals", qrow(qg)[0] == "WITHDRAWN" and qrow(qg)[2] == J1)
        r1, r2 = patch(qg, {"actor": OTHER, "justification": "x"}), submit(qg, actor=OTHER)
        check("a non-creator on a non-DRAFT QCS -> 403 first (403 before 409)",
              r1.status_code == 403 and r2.status_code == 403 and code(r1) == code(r2) == "NOT_THE_CREATOR")
        check("submit of an unknown QCS -> 404", submit(999999).status_code == 404)

        rfqX, qx, _p = fresh_draft()
        assert client.post(f"/api/rfq/{rfqX}/cancel", json={"actor": BUYER, "reason": "re-tender"}).status_code == 200
        r1, r2 = patch(qx, {"actor": BUYER, "justification": "new text"}), submit(qx)
        check("a DRAFT QCS on a CANCELLED RFQ: edit and submit -> 409 RFQ_NOT_CLOSED (+ rfq_status)",
              r1.status_code == 409 and code(r1) == "RFQ_NOT_CLOSED" and detail_of(r1).get("rfq_status") == "CANCELLED"
              and r2.status_code == 409 and code(r2) == "RFQ_NOT_CLOSED" and detail_of(r2).get("rfq_status") == "CANCELLED")
        r = patch(qx, {"actor": OTHER, "justification": "x"})
        check("a non-creator there still gets 403 first", r.status_code == 403 and code(r) == "NOT_THE_CREATOR")

        rfqN = closed_rfq()
        with SessionLocal() as s:   # a QCS with NO creator, as direct SQL could leave it
            ghost = QCS(rfq_id=rfqN, status="DRAFT", recommended_quotation_id=qid(rfqN, 1), justification=J1)
            s.add(ghost)
            s.commit()
            ghost_id = ghost.id
        r1, r2 = patch(ghost_id, {"actor": BUYER, "justification": "x"}), submit(ghost_id)
        check("a QCS without a creator can be changed by nobody (403)",
              r1.status_code == 403 and r2.status_code == 403 and qrow(ghost_id)[0] == "DRAFT")

        print("--- D65: submit rules (the justification matrix)")
        rfq0 = closed_rfq()
        q0 = new_draft(rfq0)
        r = submit(q0)
        check("no recommended quotation -> 422 RECOMMENDATION_REQUIRED", r.status_code == 422 and code(r) == "RECOMMENDATION_REQUIRED")
        q0b = new_draft(closed_rfq(), pick=None)
        db_set_qcs(q0b, recommended_quotation_id=None)
        rfq1 = closed_rfq()
        q1 = new_draft(rfq1, pick=qid(rfq1, 1))
        r = submit(q1)
        check("a pick but no justification -> 422 JUSTIFICATION_REQUIRED", r.status_code == 422 and code(r) == "JUSTIFICATION_REQUIRED")
        check("...both refusals left DRAFT and wrote no audit row",
              qrow(q0)[0] == "DRAFT" and qrow(q1)[0] == "DRAFT" and n_audit(q0, "QCS_SUBMITTED") == 0 and n_audit(q1, "QCS_SUBMITTED") == 0)
        length_case("lowest and full", closed_rfq(), 1, 20, [])
        length_case("not the lowest", closed_rfq(), 2, 80, ["NOT_LOWEST_UNIT_PRICE"])
        length_case("partial but lowest", closed_rfq(PARTIAL_LOWEST), 2, 80, ["PARTIAL_QUOTE"])
        length_case("partial and not lowest", closed_rfq(), 3, 80, ["NOT_LOWEST_UNIT_PRICE", "PARTIAL_QUOTE"])

        print("--- D52: a successful submit")
        rfqS, qs, pS = fresh_draft()
        r = submit(qs, actor=f"  {BUYER.upper()}  ", corr="corr-3fb-2")
        j = r.json() if r.status_code == 200 else {}
        check("200 PENDING_APPROVAL; name ignores case and spaces", r.status_code == 200 and j.get("status") == "PENDING_APPROVAL",
              f"status={r.status_code} {detail_of(r)}")
        rows = audit_rows(qs, "QCS_SUBMITTED")
        before, after = (svc.json.loads(rows[0].before_state), svc.json.loads(rows[0].after_state)) if rows else ({}, {})
        check("one QCS_SUBMITTED row: before DRAFT, after PENDING with the flags, minimum and levels",
              len(rows) == 1 and before == {"status": "DRAFT"} and after.get("status") == "PENDING_APPROVAL"
              and after.get("recommended_quotation_id") == pS and after.get("is_lowest_unit_price_incl_tax") is True
              and after.get("is_partial") is False and after.get("justification_min_length") == 20
              and after.get("reasons") == [] and after.get("levels_required") == 1)
        check("audit: cleaned actor and correlation id", bool(rows) and rows[0].actor == BUYER.upper()
              and rows[0].correlation_id == "corr-3fb-2")
        check("GET shows the new status", client.get(f"/api/qcs/{qs}").json().get("status") == "PENDING_APPROVAL")
        snap = total_rows()
        r = submit(qs)
        check("submit again -> 409 QCS_NOT_DRAFT (+ qcs_status PENDING_APPROVAL)",
              r.status_code == 409 and code(r) == "QCS_NOT_DRAFT" and detail_of(r).get("qcs_status") == "PENDING_APPROVAL")
        r = patch(qs, {"actor": BUYER, "justification": "late change"})
        check("edit after submit -> 409 QCS_NOT_DRAFT; justification unchanged",
              r.status_code == 409 and code(r) == "QCS_NOT_DRAFT" and qrow(qs)[2] == J1)
        check("...refusals wrote nothing", total_rows() == snap)
        r = client.post(f"/api/rfq/{rfqS}/cancel", json={"actor": BUYER, "reason": "too late"})
        check("a PENDING_APPROVAL QCS blocks cancelling its RFQ (3e rule intact)", r.status_code == 409 and code(r) == "QCS_IN_PROGRESS")

        print("--- races")
        rfqR, qr, _p = fresh_draft()
        out = run_race(10, lambda i, c: submit(qr, c=c))
        n200 = sum(r.status_code == 200 for r in out)
        refused = [r for r in out if r.status_code == 409]
        others = [r.status_code for r in out if r.status_code not in (200, 409)]
        check("10 simultaneous submits: one 200, nine 409", n200 == 1 and len(refused) == 9 and not others,
              f"200s={n200} 409s={len(refused)} others={others}")
        check("...refusals say QCS_NOT_DRAFT with PENDING_APPROVAL; one audit row",
              all(code(r) == "QCS_NOT_DRAFT" and detail_of(r).get("qcs_status") == "PENDING_APPROVAL" for r in refused)
              and n_audit(qr, "QCS_SUBMITTED") == 1)
        rfqP, qp, _p = fresh_draft()
        texts = [f"race edit number {i}, long enough" for i in range(10)]
        out = run_race(10, lambda i, c: patch(qp, {"actor": BUYER, "justification": texts[i]}, c=c))
        n200 = sum(r.status_code == 200 for r in out)
        refused = [r for r in out if r.status_code == 409]
        others = [r.status_code for r in out if r.status_code not in (200, 409)]
        check("10 simultaneous edits: only 200 or 409, at least one 200", n200 >= 1 and n200 + len(refused) == 10 and not others,
              f"200s={n200} 409s={len(refused)} others={others}")
        check("...every refusal says QCS_CHANGED", all(code(r) == "QCS_CHANGED" for r in refused))
        check("...one audit row per successful edit; the final text is one that was sent",
              n_audit(qp, "QCS_UPDATED") == n200 and qrow(qp)[2] in texts)

        print("--- D66: a race is named by the state that actually won (the UPDATE is atomic)")
        rfq, q_, pick = fresh_draft()
        r, fired = run_forced(lambda: submit(q_), lambda: db_set_rfq(rfq, status="CANCELLED"))
        check("submit while the RFQ gets cancelled -> 409 RFQ_NOT_CLOSED (CANCELLED)",
              fired and r.status_code == 409 and code(r) == "RFQ_NOT_CLOSED" and detail_of(r).get("rfq_status") == "CANCELLED",
              f"fired={fired} status={r.status_code} code={code(r)}")
        check("...still DRAFT, no QCS_SUBMITTED row", qrow(q_)[0] == "DRAFT" and n_audit(q_, "QCS_SUBMITTED") == 0)

        rfq, q_, pick = fresh_draft()
        theirs = "edited behind your back, but still long enough to look valid"
        r, fired = run_forced(lambda: submit(q_), lambda: db_set_qcs(q_, justification=theirs))
        check("submit while the justification is edited -> 409 QCS_CHANGED",
              fired and r.status_code == 409 and code(r) == "QCS_CHANGED", f"fired={fired} status={r.status_code} code={code(r)}")
        check("...still DRAFT with THEIR text (the validated one was not submitted)",
              qrow(q_)[0] == "DRAFT" and qrow(q_)[2] == theirs and n_audit(q_, "QCS_SUBMITTED") == 0)

        rfq, q_, pick = fresh_draft()
        r, fired = run_forced(lambda: submit(q_), lambda: db_set_qcs(q_, status="PENDING_APPROVAL"))
        check("submit while another submit wins -> 409 QCS_NOT_DRAFT (PENDING_APPROVAL)",
              fired and r.status_code == 409 and code(r) == "QCS_NOT_DRAFT"
              and detail_of(r).get("qcs_status") == "PENDING_APPROVAL", f"fired={fired} status={r.status_code} code={code(r)}")
        check("...this request wrote no audit row", n_audit(q_, "QCS_SUBMITTED") == 0)

        rfq, q_, pick = fresh_draft()
        r, fired = run_forced(lambda: submit(q_), lambda: db_set_quote(pick, status="SUPERSEDED"))
        check("submit while the picked quotation stops being ACTIVE -> 422 QUOTATION_NOT_ACTIVE",
              fired and r.status_code == 422 and code(r) == "QUOTATION_NOT_ACTIVE", f"fired={fired} status={r.status_code} code={code(r)}")
        check("...still DRAFT, no QCS_SUBMITTED row", qrow(q_)[0] == "DRAFT" and n_audit(q_, "QCS_SUBMITTED") == 0)

        rfq, q_, pick = fresh_draft()
        r, fired = run_forced(lambda: patch(q_, {"actor": BUYER, "justification": J2}), lambda: db_set_rfq(rfq, status="CANCELLED"))
        check("edit while the RFQ gets cancelled -> 409 RFQ_NOT_CLOSED",
              fired and r.status_code == 409 and code(r) == "RFQ_NOT_CLOSED", f"fired={fired} status={r.status_code} code={code(r)}")
        check("...the justification is unchanged, no audit row", qrow(q_)[2] == J1 and n_audit(q_, "QCS_UPDATED") == 0)

        rfq, q_, pick = fresh_draft()
        r, fired = run_forced(lambda: patch(q_, {"actor": BUYER, "justification": J2}), lambda: db_set_qcs(q_, status="PENDING_APPROVAL"))
        check("edit while the QCS gets submitted -> 409 QCS_NOT_DRAFT",
              fired and r.status_code == 409 and code(r) == "QCS_NOT_DRAFT", f"fired={fired} status={r.status_code} code={code(r)}")
        check("...the edit was not applied", qrow(q_)[2] == J1 and n_audit(q_, "QCS_UPDATED") == 0)

        rfq, q_, pick = fresh_draft()
        r, fired = run_forced(lambda: patch(q_, {"actor": BUYER, "justification": J2}), lambda: db_set_qcs(q_, justification=theirs))
        check("edit while someone else edits the same field -> 409 QCS_CHANGED",
              fired and r.status_code == 409 and code(r) == "QCS_CHANGED", f"fired={fired} status={r.status_code} code={code(r)}")
        check("...their text stays (not overwritten), no audit row", qrow(q_)[2] == theirs and n_audit(q_, "QCS_UPDATED") == 0)

        print("--- D14: audit failure rolls the change back")

        def boom(*_a, **_k):
            raise RuntimeError("audit is down")

        rfq, q_, pick = fresh_draft()
        svc.record_audit = boom
        r = patch(q_, {"actor": BUYER, "justification": J2}, c=quiet)
        svc.record_audit = original_audit
        check("audit fails -> edit fails (500), the justification is unchanged", r.status_code == 500 and qrow(q_)[2] == J1,
              f"status={r.status_code}")
        check("...and the same edit works afterwards", patch(q_, {"actor": BUYER, "justification": J2}).status_code == 200)
        svc.record_audit = boom
        r = submit(q_, c=quiet)
        svc.record_audit = original_audit
        check("audit fails -> submit fails (500), the QCS is still DRAFT, no audit row",
              r.status_code == 500 and qrow(q_)[0] == "DRAFT" and n_audit(q_, "QCS_SUBMITTED") == 0, f"status={r.status_code}")
        check("...and the same submit works afterwards", submit(q_).status_code == 200)

        print("--- D63: the service does not trust the schema for blank names")
        rfqD = closed_rfq()
        with SessionLocal() as s:
            codes = []
            for fn in (
                lambda: svc.create_qcs(s, rfqD, QCSCreate.model_construct(created_by="   ", recommended_quotation_id=None, justification=None)),
                lambda: svc.update_qcs(s, qa, QCSUpdate.model_construct(actor="  ", justification="y")),
                lambda: svc.submit_qcs(s, qa, QCSSubmit.model_construct(actor="")),
            ):
                try:
                    fn()
                    codes.append(None)
                except svc.QCSInvalid as exc:
                    codes.append(exc.code)
        check("blank created_by / actor straight into the service -> NAME_REQUIRED (x3)", codes == ["NAME_REQUIRED"] * 3, str(codes))
        check("...nothing was written", n_qcs(rfqD) == 0 and qrow(qa)[2] == J2)
    finally:
        Session.execute = original_execute
        hook["fn"] = None
        svc.record_audit = original_audit
        purge()

    after_total = total_rows()
    with SessionLocal() as s:
        leftover = s.scalar(select(func.count()).select_from(Vendor).where(Vendor.vendor_id.like("CHK3B%")))
    print(f"\nrows before={before_total} after={after_total}  leftover test vendors={leftover}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total and leftover == 0 else "SOMETHING FAILED")


if __name__ == "__main__":
    main()