"""Prove Phase 3d: vendor quotations with revisions (D34-D41 and the clarifications).

Run from backend/ (after seed_vendors and seed_items; needs httpx installed):
    python -m scripts.check_quotation_api

Creates PRs tagged requester 'check.quote.3d' and vendors whose ids start with 'CHK3D'; everything,
including audit rows, is deleted at the end.
"""
import json
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

import app.core.rules as rules
from app.db.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Item, Quotation, RFQ, Vendor
from scripts._helpers import cleanup, new_quote, total_rows

TAG = "check.quote.3d"
ACTOR = f"{TAG}.buyer"
client = TestClient(app)
results: list[bool] = []
TODAY = date.today()
FUTURE = (TODAY + timedelta(days=30)).isoformat()
IN_7 = (TODAY + timedelta(days=7)).isoformat()
V = {i: f"CHK3D{i:02d}" for i in range(1, 6)}
V_NOT_INVITED = "CHK3DNI"
_n = {"pr": 0}


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label:66} {detail}")
    return ok


def code(r):
    d = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    return d.get("code") if isinstance(d, dict) else None


def detail_of(r) -> dict:
    d = r.json().get("detail")
    return d if isinstance(d, dict) else {}


def published_rfq(quantity=100, vendors=(1, 2, 3, 4), deadline=IN_7, publish=True) -> int:
    _n["pr"] += 1
    body = dict(requester=TAG, department="Procurement", item_id="I001", quantity=quantity,
                estimated_budget="50000.10", currency="INR", required_delivery_date=FUTURE)
    pr = client.post("/api/requisitions", json=body, headers={"Idempotency-Key": f"chk-3d-pr-{_n['pr']}"}).json()["id"]
    assert client.post(f"/api/requisitions/{pr}/decision", json={"approver": "mgr.3d", "decision": "APPROVED"}).status_code == 201
    rfq = client.post("/api/rfq/generate", json={"pr_id": pr, "actor": ACTOR}).json()["id"]
    if publish:
        r = client.post(f"/api/rfq/{rfq}/publish", json={"actor": ACTOR, "vendor_ids": [V[i] for i in vendors], "response_deadline": deadline})
        assert r.status_code == 200, r.text
    return rfq


def body_for(vendor, qty=60, price="4200.00", tax="18.00", lead=14, **kw) -> dict:
    data = {"vendor_id": vendor, "quoted_quantity": qty, "unit_price": price, "tax_rate_percent": tax,
            "currency": "INR", "lead_time_days": lead}
    data.update(kw)
    return data


def quote(rfq_id, vendor, key=None, corr=None, c=client, body=None, raw=None, **kw):
    headers = {}
    if key is not None:
        headers["Idempotency-Key"] = key
    if corr is not None:
        headers["X-Correlation-ID"] = corr
    if raw is not None:
        headers["Content-Type"] = "application/json"
        return c.post(f"/api/rfq/{rfq_id}/quotations", content=raw, headers=headers)
    return c.post(f"/api/rfq/{rfq_id}/quotations", json=body if body is not None else body_for(vendor, **kw), headers=headers)


def rows(rfq_id, vendor=None) -> list:
    with SessionLocal() as s:
        stmt = select(Quotation.vendor_id, Quotation.revision, Quotation.status).where(Quotation.rfq_id == rfq_id)
        if vendor:
            stmt = stmt.where(Quotation.vendor_id == vendor)
        return [(r.vendor_id, r.revision, r.status) for r in s.execute(stmt.order_by(Quotation.vendor_id, Quotation.revision))]


def vrows(rfq_id, vendor) -> list:
    return [(rev, st) for _v, rev, st in rows(rfq_id, vendor)]


def n_audit(quotation_id, action) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.entity_type == "QUOTATION", AuditEvent.entity_id == str(quotation_id), AuditEvent.action == action))


def set_rfq(rfq_id, **values) -> None:
    with SessionLocal() as s:
        s.execute(update(RFQ).where(RFQ.id == rfq_id).values(**values))
        s.commit()


def past_deadline(rfq_id) -> None:
    set_rfq(rfq_id, issue_date=TODAY - timedelta(days=10), response_deadline=TODAY - timedelta(days=1))


def rejects(label, rfq_id, why, expected=422, check_code=None, vendor=None, key=None, **kw) -> None:
    """Must be refused with `expected`, for the right reason, writing nothing."""
    before = rows(rfq_id)
    r = quote(rfq_id, vendor or V[1], key=key, **kw)
    detail = json.dumps(r.json().get("detail", ""))
    ok = r.status_code == expected and why in detail and rows(rfq_id) == before
    if check_code:
        ok = ok and code(r) == check_code
    check(label, ok, f"status={r.status_code} code={code(r)}")


def run_race(n, fn) -> list:
    out, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker(i):
        c = TestClient(app)
        barrier.wait()
        r = fn(i, c)
        with lock:
            out.append(r)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def main() -> None:
    before_total = total_rows()
    original_rules = (rules.SEAL_QUOTATIONS_UNTIL_DEADLINE, rules.MAX_LEAD_TIME_DAYS)
    original_audit = None
    with SessionLocal() as s:
        category = s.scalar(select(Item.category).where(Item.item_id == "I001"))
        for vid in list(V.values()) + [V_NOT_INVITED]:
            s.add(Vendor(vendor_id=vid, vendor_name=f"Check {vid}", category=category, oem_approved=True, is_active=True))
        s.commit()
    try:
        print("--- D36: submit, and the server computes the money")
        r1 = published_rfq(quantity=100)
        r = quote(r1, V[1], corr="wf-3d-1")
        j = r.json() if r.status_code == 201 else {}
        check("first quotation -> 201, revision 1, ACTIVE", r.status_code == 201 and j.get("revision") == 1 and j.get("status") == "ACTIVE", f"status={r.status_code}")
        check("partial quantity (60 of 100) is fine; money computed as strings", (j.get("net_amount"), j.get("tax_amount"), j.get("total_amount"), j.get("unit_price")) == ("252000.00", "45360.00", "297360.00", "4200.00"), f"{j.get('net_amount')}/{j.get('tax_amount')}/{j.get('total_amount')}")
        q1 = j.get("id")
        with SessionLocal() as s:
            ev = s.scalars(select(AuditEvent).where(AuditEvent.entity_type == "QUOTATION", AuditEvent.entity_id == str(q1))).all()
        check("one QUOTATION_SUBMITTED audit row, actor = vendor, total in after-state", len(ev) == 1 and ev[0].action == "QUOTATION_SUBMITTED" and ev[0].actor == V[1] and json.loads(ev[0].after_state)["total_amount"] == "297360.00" and ev[0].before_state is None and ev[0].correlation_id == "wf-3d-1")
        r = quote(r1, V[2], qty=3, price="33.33")
        check("tax rounding half-up: 3 x 33.33 @18% -> tax 18.00, total 117.99", r.status_code == 201 and (r.json()["tax_amount"], r.json()["total_amount"]) == ("18.00", "117.99"))
        r = quote(r1, V[3], currency="inr", payment_terms="   ", source_document_ref="  q.pdf ")
        j3 = r.json() if r.status_code == 201 else {}
        check("currency normalised, blank terms -> null, reference trimmed", r.status_code == 201 and j3.get("currency") == "INR" and j3.get("payment_terms") is None and j3.get("source_document_ref") == "q.pdf")
        before = rows(r1)
        for field in ("net_amount", "tax_amount", "total_amount"):
            r = quote(r1, V[4], body={**body_for(V[4]), field: "1.00"})
            check(f"client-sent {field} is rejected, not trusted", r.status_code == 422 and field in json.dumps(r.json()) and rows(r1) == before)
        g = client.get(f"/api/quotations/{q1}")
        check("GET /api/quotations/{id}", g.status_code == 200 and g.json()["total_amount"] == "297360.00")

        print("\n--- D37: who and when")
        r = quote(r1, V_NOT_INVITED)
        check("a vendor that was not invited -> 403 NOT_INVITED", r.status_code == 403 and code(r) == "NOT_INVITED")
        r = quote(r1, "NOPE99")
        check("an unknown vendor -> 403 NOT_INVITED", r.status_code == 403 and code(r) == "NOT_INVITED")
        r = quote(999999, V[1])
        check("unknown RFQ -> 404 RFQ_NOT_FOUND", r.status_code == 404 and code(r) == "RFQ_NOT_FOUND")
        draft = published_rfq(publish=False)
        r = quote(draft, V[1])
        check("a DRAFT RFQ -> 409 RFQ_NOT_ACCEPTING (+ status)", r.status_code == 409 and code(r) == "RFQ_NOT_ACCEPTING" and detail_of(r).get("rfq_status") == "DRAFT")
        cancelled = published_rfq()
        client.post(f"/api/rfq/{cancelled}/cancel", json={"actor": ACTOR, "reason": "test"})
        r = quote(cancelled, V[1])
        check("a CANCELLED RFQ -> 409 RFQ_NOT_ACCEPTING", r.status_code == 409 and detail_of(r).get("rfq_status") == "CANCELLED")

        print("\n--- D37: validation (right reason, nothing written)")
        rejects("quantity 0", r1, "quoted_quantity", qty=0, vendor=V[4])
        rejects("quantity true", r1, "quoted_quantity", qty=True, vendor=V[4])
        rejects("quantity 5.5", r1, "quoted_quantity", qty=5.5, vendor=V[4])
        rejects("quantity as a string", r1, "quoted_quantity", qty="5", vendor=V[4])
        rejects("quantity above the RFQ's 100", r1, "requested_quantity", qty=101, vendor=V[4], check_code="QUANTITY_EXCEEDS_REQUEST")
        rejects("negative unit price", r1, "unit_price", price="-1", vendor=V[4])
        rejects("unit price with 3 decimals", r1, "unit_price", price="10.123", vendor=V[4])
        rejects("unit price NaN", r1, "unit_price", price="NaN", vendor=V[4])
        rejects("tax rate 101", r1, "tax_rate_percent", tax="101", vendor=V[4])
        rejects("negative tax rate", r1, "tax_rate_percent", tax="-1", vendor=V[4])
        rejects("tax rate with 3 decimals", r1, "tax_rate_percent", tax="18.123", vendor=V[4])
        rejects("currency USD", r1, "unsupported currency", currency="USD", vendor=V[4])
        rejects("lead time -1", r1, "lead_time_days", lead=-1, vendor=V[4])
        rejects("lead time 366 (default maximum 365)", r1, "lead_time_days", lead=366, vendor=V[4])
        rejects("lead time as a string", r1, "lead_time_days", lead="14", vendor=V[4])
        rejects("payment terms over 50 characters", r1, "payment_terms", payment_terms="x" * 51, vendor=V[4])
        rejects("source reference over 255 characters", r1, "source_document_ref", source_document_ref="x" * 256, vendor=V[4])
        rejects("extra field 'status'", r1, "status", vendor=V[4], body={**body_for(V[4]), "status": "ACTIVE"})
        rejects("extra field 'revision'", r1, "revision", vendor=V[4], body={**body_for(V[4]), "revision": 9})
        rejects("missing vendor_id", r1, "vendor_id", body={k: v for k, v in body_for(V[4]).items() if k != "vendor_id"})
        rejects("amount too large to store", r1, "limit", qty=100, price="99999999999.99", vendor=V[4], check_code="AMOUNT_TOO_LARGE")
        rejects("bad Idempotency-Key", r1, "Idempotency-Key", vendor=V[4], key="a b")
        rejects("not JSON", r1, "json", vendor=V[4], raw="nope")
        rules.MAX_LEAD_TIME_DAYS = 30
        rejects("lead time 31 when the limit is configured to 30", r1, "30", lead=31, vendor=V[4])
        r = quote(r1, V[4], lead=30)
        check("...and 30 is accepted (the limit is configurable)", r.status_code == 201)
        rules.MAX_LEAD_TIME_DAYS = original_rules[1]

        print("\n--- clarification B: the deadline (inclusive) and revisions")
        r2 = published_rfq(quantity=50, vendors=(1, 2))
        a = quote(r2, V[1], qty=50, key="k-3d-late")
        check("before the deadline: V1 submits", a.status_code == 201)
        past_deadline(r2)
        before = rows(r2)
        r = quote(r2, V[1], qty=50, price="4100.00")
        check("a REVISION after the deadline -> 409 RFQ_DEADLINE_PASSED (+ the deadline)", r.status_code == 409 and code(r) == "RFQ_DEADLINE_PASSED" and detail_of(r).get("response_deadline") == (TODAY - timedelta(days=1)).isoformat())
        r = quote(r2, V[2], qty=50)
        check("a NEW quotation after the deadline -> 409", r.status_code == 409 and code(r) == "RFQ_DEADLINE_PASSED")
        r = quote(r2, V[1], qty=50)
        check("identical content after the deadline, no key -> still 409 (strict)", r.status_code == 409 and code(r) == "RFQ_DEADLINE_PASSED")
        r = quote(r2, V[1], qty=50, key="k-3d-late")
        check("a retry with the KNOWN key after the deadline -> 200 original", r.status_code == 200 and r.json().get("id") == a.json().get("id") and r.headers.get("Idempotent-Replay") == "true")
        check("...nothing was written", rows(r2) == before)
        r3 = published_rfq(quantity=50, vendors=(1,))
        set_rfq(r3, issue_date=TODAY, response_deadline=TODAY)
        check("the deadline day itself is still open (inclusive)", quote(r3, V[1], qty=50).status_code == 201)

        print("\n--- D38: revisions keep the history, one ACTIVE per vendor")
        r4 = published_rfq(quantity=100, vendors=(1, 2))
        q_a = quote(r4, V[1]).json()
        quote(r4, V[2])
        r = quote(r4, V[1], price="4100.00", corr="wf-3d-4")
        j = r.json() if r.status_code == 201 else {}
        check("second submission -> 201 revision 2, ACTIVE", r.status_code == 201 and j.get("revision") == 2 and j.get("status") == "ACTIVE" and j.get("id") != q_a["id"], f"status={r.status_code}")
        check("revision 1 is now SUPERSEDED, only revision 2 is ACTIVE", vrows(r4, V[1]) == [(1, "SUPERSEDED"), (2, "ACTIVE")], f"{vrows(r4, V[1])}")
        check("the other vendor's quotation is untouched", vrows(r4, V[2]) == [(1, "ACTIVE")])
        with SessionLocal() as s:
            ev = s.scalars(select(AuditEvent).where(AuditEvent.entity_type == "QUOTATION", AuditEvent.entity_id == str(j.get("id")))).one()
        check("QUOTATION_REVISED audit: before = revision 1, after = revision 2", ev.action == "QUOTATION_REVISED" and json.loads(ev.before_state)["revision"] == 1 and json.loads(ev.before_state)["id"] == q_a["id"] and json.loads(ev.after_state)["revision"] == 2 and ev.correlation_id == "wf-3d-4")
        quote(r4, V[1], qty=55, price="4100.00")
        check("third submission -> revision 3; both older revisions SUPERSEDED", vrows(r4, V[1]) == [(1, "SUPERSEDED"), (2, "SUPERSEDED"), (3, "ACTIVE")], f"{vrows(r4, V[1])}")
        cur = client.get(f"/api/rfq/{r4}/quotations").json()
        rev3 = [x for x in cur if x["vendor_id"] == V[1]][0]
        r = quote(r4, V[1], qty=55, price="4100.00")
        check("identical to the current ACTIVE (no key) -> 200, same quotation, no new revision", r.status_code == 200 and r.json()["id"] == rev3["id"] and r.headers.get("Idempotent-Replay") == "true" and len(vrows(r4, V[1])) == 3)
        r = quote(r4, V[1], qty=55, price="4100.00", key="k-3d-new-key")
        check("identical content with a NEW key -> 200, no new revision", r.status_code == 200 and r.json()["id"] == rev3["id"] and len(vrows(r4, V[1])) == 3)
        r = quote(r4, V[1], qty=55, price="4100.0", currency="inr", payment_terms="  ")
        check("same content, different formatting -> 200 no-op", r.status_code == 200 and r.json()["id"] == rev3["id"])
        check("no extra audit rows for the no-ops", n_audit(rev3["id"], "QUOTATION_REVISED") == 1 and n_audit(rev3["id"], "QUOTATION_SUBMITTED") == 0)
        check("GET list = ACTIVE only (V1 rev 3, V2 rev 1)", sorted((x["vendor_id"], x["revision"]) for x in cur) == [(V[1], 3), (V[2], 1)])
        hist = client.get(f"/api/rfq/{r4}/quotations?include_history=true").json()
        check("include_history=true returns every revision in order", [(x["vendor_id"], x["revision"], x["status"]) for x in hist] == [(V[1], 1, "SUPERSEDED"), (V[1], 2, "SUPERSEDED"), (V[1], 3, "ACTIVE"), (V[2], 1, "ACTIVE")])
        check("a SUPERSEDED revision can still be read by id", client.get(f"/api/quotations/{q_a['id']}").json().get("status") == "SUPERSEDED")
        check("GET unknown quotation -> 404", client.get("/api/quotations/999999").status_code == 404 and client.get("/api/rfq/999999/quotations").status_code == 404)

        print("\n--- clarification A: idempotency is scoped to RFQ + vendor + key")
        r5 = published_rfq(quantity=100, vendors=(1, 2))
        a1, a2 = quote(r5, V[1], key="QUOTE-001"), quote(r5, V[2], key="QUOTE-001")
        check("two vendors both use QUOTE-001 on one RFQ -> both 201, no conflict", a1.status_code == 201 and a2.status_code == 201 and a1.json()["id"] != a2.json()["id"])
        r = quote(r5, V[1], key="QUOTE-001")
        check("same vendor, same key, same body -> 200 original", r.status_code == 200 and r.json()["id"] == a1.json()["id"] and r.headers.get("Idempotent-Replay") == "true")
        before = rows(r5)
        r = quote(r5, V[1], key="QUOTE-001", price="9999.00")
        check("same vendor, same key, CHANGED body -> 409 IDEMPOTENCY_KEY_REUSED", r.status_code == 409 and code(r) == "IDEMPOTENCY_KEY_REUSED" and rows(r5) == before)
        r = quote(r5, V[1], key="QUOTE-001", price="4200.0", currency="inr")
        check("same key, equivalent formatting -> 200", r.status_code == 200 and r.json()["id"] == a1.json()["id"])
        r6 = published_rfq(quantity=100, vendors=(1,))
        check("same key on a DIFFERENT RFQ is a different request -> 201", quote(r6, V[1], key="QUOTE-001").status_code == 201)
        rev1 = quote(r5, V[2], key="K1", qty=40).json()   # V2: current ACTIVE is a2; this is a new revision with key K1
        quote(r5, V[2], key="K2", qty=41)
        r = quote(r5, V[2], key="K1", qty=40)
        check("a late retry of an OLD revision returns THAT revision (SUPERSEDED)", r.status_code == 200 and r.json()["id"] == rev1["id"] and r.json()["revision"] == 2 and r.json()["status"] == "SUPERSEDED", f"status={r.status_code} rev={r.json().get('revision')} st={r.json().get('status')}")
        check("...and created nothing new; the newest revision stays ACTIVE", vrows(r5, V[2]) == [(1, "SUPERSEDED"), (2, "SUPERSEDED"), (3, "ACTIVE")], f"{vrows(r5, V[2])}")

        print("\n--- clarification C: sealed vs unsealed (the switch is off by default)")
        check("default is unsealed", rules.SEAL_QUOTATIONS_UNTIL_DEADLINE is False and client.get(f"/api/rfq/{r5}/quotations").status_code == 200)
        r7 = published_rfq(quantity=100, vendors=(1, 2))
        rules.SEAL_QUOTATIONS_UNTIL_DEADLINE = True
        r = quote(r7, V[1])
        own = r.json() if r.status_code == 201 else {}
        check("sealed: a vendor can still SUBMIT and gets its own quotation back", r.status_code == 201 and own.get("total_amount") == "297360.00")
        r = client.get(f"/api/rfq/{r7}/quotations")
        check("sealed: the list is 403 QUOTATIONS_SEALED (+ the deadline)", r.status_code == 403 and code(r) == "QUOTATIONS_SEALED" and detail_of(r).get("response_deadline") == IN_7)
        check("sealed: the message says it applies to everyone", "everyone" in detail_of(r).get("message", ""))
        check("sealed: include_history is sealed too", client.get(f"/api/rfq/{r7}/quotations?include_history=true").status_code == 403)
        check("sealed: GET by id is sealed too", client.get(f"/api/quotations/{own['id']}").status_code == 403)
        check("sealed: an unknown RFQ is still 404, not masked as 403", client.get("/api/rfq/999999/quotations").status_code == 404)
        set_rfq(r7, issue_date=TODAY, response_deadline=TODAY)
        check("sealed: on the deadline day it is still sealed", client.get(f"/api/rfq/{r7}/quotations").status_code == 403)
        past_deadline(r7)
        r = client.get(f"/api/rfq/{r7}/quotations")
        check("sealed: after the deadline the list opens (buyer evaluation works)", r.status_code == 200 and len(r.json()) == 1)
        check("sealed: after the deadline GET by id opens", client.get(f"/api/quotations/{own['id']}").status_code == 200)
        r8 = published_rfq(quantity=100, vendors=(1,))
        quote(r8, V[1])
        check("sealed: still sealed before the deadline", client.get(f"/api/rfq/{r8}/quotations").status_code == 403)
        set_rfq(r8, status="CLOSED")
        check("sealed: a CLOSED RFQ is open even before its deadline date", client.get(f"/api/rfq/{r8}/quotations").status_code == 200)
        rules.SEAL_QUOTATIONS_UNTIL_DEADLINE = False
        r9 = published_rfq(quantity=100, vendors=(1,))
        quote(r9, V[1])
        check("unsealed: readable before the deadline (competitor prices visible, disclosed A66)", client.get(f"/api/rfq/{r9}/quotations").status_code == 200)

        print("\n--- the database enforces it too (bypassing the service)")
        r10 = published_rfq(quantity=10, vendors=(1, 2))

        def direct(label, build, expect):
            with SessionLocal() as s:
                try:
                    build(s)
                    s.commit()
                    outcome, ok = "accepted", expect is None
                except IntegrityError as exc:
                    s.rollback()
                    outcome, ok = f"rejected: {str(exc.orig)[:52]}", expect is not None and expect in str(exc.orig)
            check(label, ok, outcome)

        direct("two ACTIVE quotations for one vendor on one RFQ", lambda s: s.add_all([new_quote(r10, V[1], revision=1), new_quote(r10, V[1], revision=2)]), "UNIQUE")
        direct("a SUPERSEDED row plus an ACTIVE row is fine", lambda s: s.add_all([new_quote(r10, V[1], revision=1, status="SUPERSEDED"), new_quote(r10, V[1], revision=2)]), None)
        direct("a reused revision number", lambda s: s.add(new_quote(r10, V[1], revision=2, status="SUPERSEDED")), "UNIQUE")
        direct("revision 0", lambda s: s.add(new_quote(r10, V[2], revision=0)), "ck_quote_revision_positive")
        direct("same key, two vendors on one RFQ is fine", lambda s: s.add_all([new_quote(r10, V[2], idempotency_key="k-db"), new_quote(r10, V[1], revision=3, status="SUPERSEDED", idempotency_key="k-db")]), None)
        direct("same key twice for one vendor on one RFQ", lambda s: s.add(new_quote(r10, V[1], revision=9, status="SUPERSEDED", idempotency_key="k-db")), "UNIQUE")
        direct("a vendor that was not invited (still a foreign key)", lambda s: s.add(new_quote(r10, V_NOT_INVITED)), "FOREIGN KEY")

        print("\n--- races")
        r11 = published_rfq(quantity=100, vendors=(1, 2, 3, 4))
        res = run_race(10, lambda i, c: quote(r11, V[1], key="k-3d-race", c=c))
        st = [x.status_code for x in res]
        check("10 retries of ONE request: one 201, nine 200", st.count(201) == 1 and st.count(200) == 9, f"201s={st.count(201)} 200s={st.count(200)} others={[s for s in st if s not in (200, 201)]}")
        check("...one row", rows(r11, V[1]) == [(V[1], 1, "ACTIVE")])
        r12 = published_rfq(quantity=100, vendors=(1,))
        res = run_race(10, lambda i, c: quote(r12, V[1], c=c))
        st = [x.status_code for x in res]
        check("10 identical submissions, no key: one 201, nine 200", st.count(201) == 1 and st.count(200) == 9, f"201s={st.count(201)} 200s={st.count(200)} others={[s for s in st if s not in (200, 201)]}")
        check("...one ACTIVE row, revision 1", vrows(r12, V[1]) == [(1, "ACTIVE")])
        r13 = published_rfq(quantity=100, vendors=(1,))
        res = run_race(10, lambda i, c: quote(r13, V[1], price=f"{4000 + i}.00", c=c))
        st = [x.status_code for x in res]
        got = vrows(r13, V[1])
        n_ok = st.count(201)
        check("10 DIFFERENT revisions at once: only 201 or 409, never an error", set(st) <= {201, 409} and n_ok >= 1, f"201s={n_ok} 409s={st.count(409)} others={[s for s in st if s not in (201, 409)]}")
        check("...exactly one ACTIVE, and one row per accepted revision", sum(1 for _r, s in got if s == "ACTIVE") == 1 and len(got) == n_ok, f"rows={len(got)} accepted={n_ok}")
        check("...revision numbers are contiguous 1..n and the newest is the ACTIVE one", [rv for rv, _s in got] == list(range(1, n_ok + 1)) and got[-1][1] == "ACTIVE", f"{got}")
        check("...every refusal says QUOTATION_CHANGED", all(code(x) == "QUOTATION_CHANGED" for x in res if x.status_code == 409))
        r14 = published_rfq(quantity=100, vendors=(1, 2, 3, 4))
        res = run_race(4, lambda i, c: quote(r14, V[i + 1], c=c))
        check("4 different vendors at once: all 201, no interference", all(x.status_code == 201 for x in res) and len(rows(r14)) == 4)

        print("\n--- D14: audit failure rolls everything back")
        import app.services.quotations as svc

        original_audit = svc.record_audit

        def boom(*a, **k):
            raise RuntimeError("audit store down")

        failing = TestClient(app, raise_server_exceptions=False)
        r15 = published_rfq(quantity=100, vendors=(1,))
        svc.record_audit = boom
        try:
            r = quote(r15, V[1], c=failing)
            check("audit fails -> submit fails (500), no quotation left", r.status_code == 500 and rows(r15) == [], f"status={r.status_code}")
        finally:
            svc.record_audit = original_audit
        check("the same vendor can submit once audit is healthy", quote(r15, V[1]).status_code == 201)
        svc.record_audit = boom
        try:
            r = quote(r15, V[1], price="3900.00", c=failing)
            check("audit fails on a REVISION -> 500, the old one stays ACTIVE", r.status_code == 500 and vrows(r15, V[1]) == [(1, "ACTIVE")], f"status={r.status_code} {vrows(r15, V[1])}")
        finally:
            svc.record_audit = original_audit
    finally:
        rules.SEAL_QUOTATIONS_UNTIL_DEADLINE, rules.MAX_LEAD_TIME_DAYS = original_rules
        if original_audit is not None:
            import app.services.quotations as svc
            svc.record_audit = original_audit
        cleanup(TAG)
        with SessionLocal() as s:
            s.execute(delete(AuditEvent).where(AuditEvent.actor.like("CHK3D%")))
            s.execute(delete(Vendor).where(Vendor.vendor_id.like("CHK3D%")))
            s.commit()

    after_total = total_rows()
    with SessionLocal() as s:
        leftover = s.scalar(select(func.count()).select_from(Vendor).where(Vendor.vendor_id.like("CHK3D%")))
    print(f"\nrows before={before_total} after={after_total}  leftover test vendors={leftover}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total and leftover == 0 else "SOMETHING FAILED")


if __name__ == "__main__":
    main()
    