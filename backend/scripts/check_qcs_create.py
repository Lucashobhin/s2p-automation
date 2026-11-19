"""Prove Slice 3f block A over HTTP: create a QCS (D48), recommendation facts (D49, D50, D53), reads.

Run from backend/ (after seed_vendors and seed_items):
    python -m scripts.check_qcs_create

Creates PRs tagged requester 'check.qcs.3f' and vendors CHK3F01..05; everything, including audit
rows, is deleted at the end (and at the start, in case an earlier run crashed).
"""
import json
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

import app.services.qcs as qcs_svc
from app.db.database import SessionLocal
from app.main import app
from app.models import QCS, AuditEvent, Item, Quotation, RFQ, Vendor
from scripts._helpers import cleanup, total_rows

TAG = "check.qcs.3f"
ACTOR = f"{TAG}.buyer"
BUYER = ACTOR
client = TestClient(app)
quiet = TestClient(app, raise_server_exceptions=False)   # an unhandled server error shows up as a 500
results: list[bool] = []
TODAY = date.today()
FUTURE = (TODAY + timedelta(days=30)).isoformat()
IN_7 = (TODAY + timedelta(days=7)).isoformat()
V = {i: f"CHK3F{i:02d}" for i in range(1, 6)}
_n = {"pr": 0}
# unit price incl. tax: V1 4720.0000 (full), V2 4956.0000 (full), V3 5310.0000 (60 of 100)
QS = [(1, 100, "4000.00"), (2, 100, "4200.00"), (3, 60, "4500.00")]


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


# ---------- fixtures (same pattern as check_rfq_close_compare) ----------

def published_rfq(quantity=100, vendors=(1, 2), publish=True) -> int:
    _n["pr"] += 1
    body = dict(requester=TAG, department="Procurement", item_id="I001", quantity=quantity,
                estimated_budget="50000.10", currency="INR", required_delivery_date=FUTURE)
    pr = client.post("/api/requisitions", json=body, headers={"Idempotency-Key": f"chk-3f-pr-{_n['pr']}"}).json()["id"]
    assert client.post(f"/api/requisitions/{pr}/decision", json={"approver": "mgr.3f", "decision": "APPROVED"}).status_code == 201
    rfq = client.post("/api/rfq/generate", json={"pr_id": pr, "actor": ACTOR}).json()["id"]
    if publish:
        r = client.post(f"/api/rfq/{rfq}/publish", json={"actor": ACTOR, "vendor_ids": [V[i] for i in vendors], "response_deadline": IN_7})
        assert r.status_code == 200, r.text
    return rfq


def quote(rfq_id, vendor_idx, qty=100, price="4200.00", lead=14):
    body = {"vendor_id": V[vendor_idx], "quoted_quantity": qty, "unit_price": price, "tax_rate_percent": "18.00",
            "currency": "INR", "lead_time_days": lead}
    r = client.post(f"/api/rfq/{rfq_id}/quotations", json=body)
    assert r.status_code in (200, 201), r.text


def past_deadline(rfq_id) -> None:
    with SessionLocal() as s:
        s.execute(update(RFQ).where(RFQ.id == rfq_id).values(
            issue_date=TODAY - timedelta(days=10), response_deadline=TODAY - timedelta(days=1)))
        s.commit()


def close_ok(rfq_id) -> None:
    past_deadline(rfq_id)
    r = client.post(f"/api/rfq/{rfq_id}/close", json={"actor": ACTOR})
    assert r.status_code == 200, r.text


def closed_rfq(quotes) -> int:
    """quotes = [(vendor_idx, quantity, net unit price)]; returns a CLOSED RFQ."""
    rfq = published_rfq(100, vendors=sorted({v for v, _q, _p in quotes}))
    for v, q, p in quotes:
        quote(rfq, v, qty=q, price=p)
    close_ok(rfq)
    return rfq


def qid(rfq_id, vendor_idx, revision=None) -> int:
    with SessionLocal() as s:
        stmt = select(Quotation.id).where(Quotation.rfq_id == rfq_id, Quotation.vendor_id == V[vendor_idx])
        stmt = stmt.where(Quotation.revision == revision) if revision else stmt.where(Quotation.status == "ACTIVE")
        return s.scalar(stmt)


def n_qcs(rfq_id) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(QCS).where(QCS.rfq_id == rfq_id))


def qcs_id_of(rfq_id) -> int:
    with SessionLocal() as s:
        return s.scalar(select(QCS.id).where(QCS.rfq_id == rfq_id))


def n_audit(qcs_id, action) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.entity_type == "QCS", AuditEvent.entity_id == str(qcs_id), AuditEvent.action == action))


def audit_row(qcs_id, action):
    with SessionLocal() as s:
        return s.scalars(select(AuditEvent).where(
            AuditEvent.entity_type == "QCS", AuditEvent.entity_id == str(qcs_id), AuditEvent.action == action)).one()


def create(rfq_id, created_by=BUYER, pick=None, just=None, key=None, corr=None, c=client):
    body = {"created_by": created_by}
    if pick is not None:
        body["recommended_quotation_id"] = pick
    if just is not None:
        body["justification"] = just
    headers = {}
    if key:
        headers["Idempotency-Key"] = key
    if corr:
        headers["X-Correlation-ID"] = corr
    return c.post(f"/api/rfq/{rfq_id}/qcs", json=body, headers=headers)


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


def scenario(label, rfq, vendor_idx, unit, lowest, partial, coverage, min_len) -> None:
    r = create(rfq, pick=qid(rfq, vendor_idx))
    j = r.json() if r.status_code == 201 else {}
    rec = j.get("recommendation") or {}
    check(f"{label}: 201 DRAFT, unit price and coverage shown",
          r.status_code == 201 and j.get("status") == "DRAFT" and rec.get("unit_price_incl_tax") == unit
          and rec.get("coverage_percent") == coverage, f"status={r.status_code} unit={rec.get('unit_price_incl_tax')}")
    check(f"{label}: lowest={lowest}, partial={partial}",
          rec.get("is_lowest_unit_price_incl_tax") is lowest and rec.get("is_partial") is partial)
    check(f"{label}: justification minimum is {min_len}", j.get("justification_min_length") == min_len,
          f"got {j.get('justification_min_length')}")


def purge() -> None:
    cleanup(TAG)
    with SessionLocal() as s:
        s.execute(delete(AuditEvent).where(AuditEvent.actor.like("CHK3F%")))
        s.execute(delete(Vendor).where(Vendor.vendor_id.like("CHK3F%")))
        s.commit()


def main() -> None:
    purge()
    before_total = total_rows()
    original_audit = qcs_svc.record_audit
    with SessionLocal() as s:
        category = s.scalar(select(Item.category).where(Item.item_id == "I001"))
        for i, vid in V.items():
            s.add(Vendor(vendor_id=vid, vendor_name=f"Check {vid}", category=category, oem_approved=(i != 5), is_active=True))
        s.commit()
    try:
        print("--- D48: only a CLOSED RFQ gets a QCS")
        r = client.post("/api/rfq/999999/qcs", json={"created_by": BUYER})
        check("unknown RFQ -> 404 RFQ_NOT_FOUND", r.status_code == 404 and code(r) == "RFQ_NOT_FOUND")
        draft = published_rfq(publish=False)
        r = create(draft)
        check("a DRAFT RFQ -> 409 RFQ_NOT_CLOSED (+ rfq_status)",
              r.status_code == 409 and code(r) == "RFQ_NOT_CLOSED" and detail_of(r).get("rfq_status") == "DRAFT")
        pub = published_rfq()
        quote(pub, 1)
        r = create(pub)
        check("a PUBLISHED RFQ -> 409 RFQ_NOT_CLOSED (+ rfq_status)",
              r.status_code == 409 and code(r) == "RFQ_NOT_CLOSED" and detail_of(r).get("rfq_status") == "PUBLISHED")
        check("...nothing was written", n_qcs(draft) == 0 and n_qcs(pub) == 0)
        assert client.post(f"/api/rfq/{draft}/cancel", json={"actor": ACTOR, "reason": "test"}).status_code == 200
        r = create(draft)
        check("a CANCELLED RFQ -> 409 RFQ_NOT_CLOSED (+ rfq_status)",
              r.status_code == 409 and code(r) == "RFQ_NOT_CLOSED" and detail_of(r).get("rfq_status") == "CANCELLED")

        print("--- reads on things that do not exist")
        r = client.get(f"/api/rfq/{draft}/qcs")
        check("RFQ without a QCS -> 404 QCS_NOT_FOUND", r.status_code == 404 and code(r) == "QCS_NOT_FOUND")
        r = client.get("/api/rfq/999999/qcs")
        check("unknown RFQ -> 404 RFQ_NOT_FOUND", r.status_code == 404 and code(r) == "RFQ_NOT_FOUND")
        r = client.get("/api/qcs/999999")
        check("unknown QCS -> 404 QCS_NOT_FOUND", r.status_code == 404 and code(r) == "QCS_NOT_FOUND")
        r = client.get("/api/qcs/999999/approvals")
        check("approvals of an unknown QCS -> 404 QCS_NOT_FOUND", r.status_code == 404 and code(r) == "QCS_NOT_FOUND")

        print("--- D49: what may be recommended, and bad input")
        rfq1 = closed_rfq(QS)                                              # S1
        rfq4 = closed_rfq([(1, 100, "4000.00"), (2, 60, "3000.00")])       # S4 (also a source of a foreign quotation)
        foreign = qid(rfq4, 2)
        r = create(rfq1, pick=foreign)
        check("a quotation of ANOTHER RFQ -> 422 QUOTATION_NOT_IN_RFQ",
              r.status_code == 422 and code(r) == "QUOTATION_NOT_IN_RFQ" and detail_of(r).get("quotation_id") == foreign)
        r = create(rfq1, pick=999999)
        check("a quotation that does not exist -> 422 QUOTATION_NOT_IN_RFQ", r.status_code == 422 and code(r) == "QUOTATION_NOT_IN_RFQ")
        rs = published_rfq(vendors=(1,))
        quote(rs, 1, price="4000.00")
        quote(rs, 1, price="3900.00")   # revision 2: revision 1 becomes SUPERSEDED
        close_ok(rs)
        old, new = qid(rs, 1, revision=1), qid(rs, 1, revision=2)
        r = create(rs, pick=old)
        check("a SUPERSEDED revision -> 422 QUOTATION_NOT_ACTIVE (+ its status)",
              r.status_code == 422 and code(r) == "QUOTATION_NOT_ACTIVE" and detail_of(r).get("quotation_status") == "SUPERSEDED")
        check("...refusals wrote nothing (no QCS rows, no audit)", n_qcs(rfq1) == 0 and n_qcs(rs) == 0)
        r = create(rs, pick=new)
        check("the ACTIVE revision of the same vendor is accepted", r.status_code == 201 and r.json()["recommendation"]["revision"] == 2)
        r = client.post(f"/api/rfq/{rfq1}/qcs", json={"created_by": "   "})
        check("blank created_by -> 422", r.status_code == 422)
        r = client.post(f"/api/rfq/{rfq1}/qcs", json={"created_by": BUYER, "status": "APPROVED"})
        check("extra field -> 422", r.status_code == 422)
        r = client.post(f"/api/rfq/{rfq1}/qcs", json={"created_by": BUYER, "recommended_quotation_id": "5"})
        check("quotation id as text -> 422 (strict)", r.status_code == 422)
        r = create(rfq1, key="bad key!")
        check("an invalid Idempotency-Key header -> 422", r.status_code == 422)
        check("...none of these wrote a QCS", n_qcs(rfq1) == 0)

        print("--- S1: create with the lowest full quote (the facts, the audit row)")
        pick1 = qid(rfq1, 1)
        r = create(rfq1, pick=pick1, just="lowest price, full coverage", corr="corr-3f-1")
        j = r.json() if r.status_code == 201 else {}
        rec = j.get("recommendation") or {}
        check("201 DRAFT with creator, RFQ facts and the pick",
              r.status_code == 201 and j.get("status") == "DRAFT" and j.get("created_by") == BUYER
              and j.get("rfq_id") == rfq1 and j.get("rfq_status") == "CLOSED" and j.get("recommended_quotation_id") == pick1,
              f"status={r.status_code}")
        check("recommendation facts: vendor, quantity, coverage", rec.get("vendor_id") == V[1]
              and rec.get("quoted_quantity") == 100 and rec.get("coverage_percent") == "100.00" and rec.get("revision") == 1)
        check("money is a STRING: unit price incl. tax and total",
              rec.get("unit_price_incl_tax") == "4720.0000" and rec.get("total_amount") == "472000.00")
        check("lowest, not partial; minimum justification 20; 1 level; 0 approved",
              rec.get("is_lowest_unit_price_incl_tax") is True and rec.get("is_partial") is False
              and j.get("justification_min_length") == 20 and j.get("levels_required") == 1 and j.get("levels_approved") == 0)
        q1 = j.get("id")
        check("exactly one QCS row and one QCS_CREATED audit row", n_qcs(rfq1) == 1 and n_audit(q1, "QCS_CREATED") == 1)
        row = audit_row(q1, "QCS_CREATED")
        after = json.loads(row.after_state)
        check("audit: actor, correlation id, no before-state, after-state holds creator and DRAFT",
              row.actor == BUYER and row.correlation_id == "corr-3f-1" and row.before_state is None
              and after.get("created_by") == BUYER and after.get("status") == "DRAFT")
        check("GET /api/qcs/{id} equals the create response", client.get(f"/api/qcs/{q1}").json() == j)
        check("GET /api/rfq/{id}/qcs equals the create response", client.get(f"/api/rfq/{rfq1}/qcs").json() == j)
        r = client.get(f"/api/qcs/{q1}/approvals")
        check("approvals of a new QCS: empty list", r.status_code == 200 and r.json() == [])
        r = create(rfq1)
        check("a second create (no key) -> 409 QCS_EXISTS (+ qcs_id, qcs_status)",
              r.status_code == 409 and code(r) == "QCS_EXISTS" and detail_of(r).get("qcs_id") == q1
              and detail_of(r).get("qcs_status") == "DRAFT")
        before_reads = total_rows()
        for u in (f"/api/qcs/{q1}", f"/api/rfq/{rfq1}/qcs", f"/api/qcs/{q1}/approvals"):
            client.get(u)
        check("reads are READ-ONLY (no rows written)", total_rows() == before_reads)

        print("--- D50: the justification rule follows the pick")
        rfq2, rfq3, rfq5 = closed_rfq(QS), closed_rfq(QS), closed_rfq([(1, 100, "4000.00"), (2, 100, "4000.00")])
        scenario("S2 not the lowest", rfq2, 2, "4956.0000", False, False, "100.00", 80)
        scenario("S3 partial and not lowest", rfq3, 3, "5310.0000", False, True, "60.00", 80)
        scenario("S4 partial but lowest", rfq4, 2, "3540.0000", True, True, "60.00", 80)
        scenario("S5 tie for lowest counts as lowest", rfq5, 2, "4720.0000", True, False, "100.00", 20)
        rfq6 = closed_rfq(QS)
        r = create(rfq6)
        j = r.json() if r.status_code == 201 else {}
        check("S6 no pick yet: 201, recommendation / min length / levels are null",
              r.status_code == 201 and j.get("recommendation") is None and j.get("justification_min_length") is None
              and j.get("levels_required") is None and j.get("levels_approved") == 0, f"status={r.status_code}")

        print("--- D48: idempotent create (replay FIRST)")
        ri = closed_rfq([(1, 100, "4000.00")])
        pi = qid(ri, 1)
        K = "chk-3f-key-1"
        r1 = create(ri, created_by=f"  {BUYER}  ", pick=pi, just="  lowest price and full coverage  ", key=K)
        check("first call -> 201; creator stored trimmed", r1.status_code == 201 and r1.json().get("created_by") == BUYER)
        qi = r1.json().get("id")
        r2 = create(ri, created_by=BUYER, pick=pi, just="lowest price and full coverage", key=K)
        check("same key, same content (trimming ignored) -> 200 replay, same QCS",
              r2.status_code == 200 and r2.json().get("id") == qi and r2.headers.get("Idempotent-Replay") == "true")
        check("...nothing new written", n_qcs(ri) == 1 and n_audit(qi, "QCS_CREATED") == 1)
        r3 = create(ri, created_by=BUYER, pick=pi, just="a different justification", key=K)
        check("same key, different content -> 409 IDEMPOTENCY_KEY_REUSED", r3.status_code == 409 and code(r3) == "IDEMPOTENCY_KEY_REUSED")
        r4 = create(ri, created_by=BUYER, key="chk-3f-key-2")
        check("another key on an RFQ that has a QCS -> 409 QCS_EXISTS",
              r4.status_code == 409 and code(r4) == "QCS_EXISTS" and detail_of(r4).get("qcs_id") == qi)
        rc = client.post(f"/api/rfq/{ri}/cancel", json={"actor": ACTOR, "reason": "re-tender"})
        check("a DRAFT QCS does not block cancelling its RFQ", rc.status_code == 200)
        r5 = create(ri, created_by=BUYER, pick=pi, just="lowest price and full coverage", key=K)
        check("replay AFTER the RFQ was cancelled -> still 200 with the original",
              r5.status_code == 200 and r5.json().get("id") == qi and r5.headers.get("Idempotent-Replay") == "true",
              f"status={r5.status_code} code={code(r5)}")
        r6 = create(ri, created_by=BUYER, pick=pi, just="changed", key=K)
        check("changed content with the old key after the cancel -> 409 IDEMPOTENCY_KEY_REUSED",
              r6.status_code == 409 and code(r6) == "IDEMPOTENCY_KEY_REUSED")
        r7 = create(ri, created_by=BUYER, key="chk-3f-key-3")
        check("a NEW request on the cancelled RFQ -> 409 RFQ_NOT_CLOSED",
              r7.status_code == 409 and code(r7) == "RFQ_NOT_CLOSED" and detail_of(r7).get("rfq_status") == "CANCELLED")
        rj = closed_rfq([(1, 100, "4000.00")])
        r8 = create(rj, key=K)
        check("the same key on ANOTHER RFQ is a different request -> 201", r8.status_code == 201 and r8.json()["rfq_id"] == rj)

        print("--- races")
        rr = closed_rfq([(1, 100, "4000.00")])
        out = run_race(10, lambda i, c: create(rr, c=c))
        n201 = sum(r.status_code == 201 for r in out)
        refused = [r for r in out if r.status_code == 409]
        others = [r.status_code for r in out if r.status_code not in (201, 409)]
        check("10 simultaneous creates: one 201, nine 409", n201 == 1 and len(refused) == 9 and not others,
              f"201s={n201} 409s={len(refused)} others={others}")
        check("...refusals say QCS_EXISTS; one QCS row, one audit row",
              all(code(r) == "QCS_EXISTS" for r in refused) and n_qcs(rr) == 1 and n_audit(qcs_id_of(rr), "QCS_CREATED") == 1)
        rk = closed_rfq([(1, 100, "4000.00")])
        out = run_race(10, lambda i, c: create(rk, key="chk-3f-race", c=c))
        n201 = sum(r.status_code == 201 for r in out)
        n200 = sum(r.status_code == 200 for r in out)
        others = [r.status_code for r in out if r.status_code not in (200, 201)]
        check("10 simultaneous creates, SAME key: one 201, nine 200 replays", n201 == 1 and n200 == 9 and not others,
              f"201s={n201} 200s={n200} others={others}")
        check("...all answers are the same QCS; one QCS row, one audit row",
              len({r.json().get("id") for r in out}) == 1 and n_qcs(rk) == 1 and n_audit(qcs_id_of(rk), "QCS_CREATED") == 1)

        print("--- D14: audit failure rolls the create back")
        rf = closed_rfq([(1, 100, "4000.00")])

        def boom(*_a, **_k):
            raise RuntimeError("audit is down")

        qcs_svc.record_audit = boom
        r = create(rf, pick=qid(rf, 1), c=quiet)
        qcs_svc.record_audit = original_audit
        check("audit fails -> create fails (500), no QCS row left", r.status_code == 500 and n_qcs(rf) == 0, f"status={r.status_code}")
        r = create(rf, pick=qid(rf, 1))
        check("...and the RFQ can still get its QCS afterwards", r.status_code == 201)

        print("--- the database enforces what the service also checks")
        rfk = closed_rfq([(1, 100, "4000.00")])
        refused_by_db = False
        with SessionLocal() as s:
            s.add(QCS(rfq_id=rfk, recommended_quotation_id=qid(rfq1, 1), status="DRAFT", created_by=BUYER))
            try:
                s.flush()
            except IntegrityError:
                refused_by_db = True
            s.rollback()
        check("a QCS pointing at another RFQ's quotation is refused by the composite FK", refused_by_db)
        check("...and nothing was stored", n_qcs(rfk) == 0)
    finally:
        qcs_svc.record_audit = original_audit
        purge()

    after_total = total_rows()
    with SessionLocal() as s:
        leftover = s.scalar(select(func.count()).select_from(Vendor).where(Vendor.vendor_id.like("CHK3F%")))
    print(f"\nrows before={before_total} after={after_total}  leftover test vendors={leftover}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total and leftover == 0 else "SOMETHING FAILED")


if __name__ == "__main__":
    main()