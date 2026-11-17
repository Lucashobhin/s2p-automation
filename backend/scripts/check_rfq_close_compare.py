"""Prove Phase 3e: close an RFQ, cancel a CLOSED RFQ (D43), and the read-only comparison (D44-D47).

Run from backend/ (after seed_vendors and seed_items; needs httpx installed):
    python -m scripts.check_rfq_close_compare

Creates PRs tagged requester 'check.close.3e' and vendors whose ids start with 'CHK3E'; everything,
including audit rows, is deleted at the end.
"""
import json
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

import app.core.rules as rules
from app.db.database import SessionLocal
from app.main import app
from app.models import QCS, AuditEvent, Item, PurchaseOrder, PurchaseRequisition, Quotation, RFQ, Vendor
from scripts._helpers import cleanup, new_po, new_quote, total_rows

TAG = "check.close.3e"
ACTOR = f"{TAG}.buyer"
client = TestClient(app)
results: list[bool] = []
TODAY = date.today()
FUTURE = (TODAY + timedelta(days=30)).isoformat()
IN_7 = (TODAY + timedelta(days=7)).isoformat()
V = {i: f"CHK3E{i:02d}" for i in range(1, 6)}
_n = {"pr": 0}
FORBIDDEN_KEYS = {"score", "rank", "ranking", "winner", "recommended", "recommendation", "best", "best_vendor"}


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


def published_rfq(quantity=100, vendors=(1, 2), deadline=IN_7, publish=True) -> int:
    _n["pr"] += 1
    body = dict(requester=TAG, department="Procurement", item_id="I001", quantity=quantity,
                estimated_budget="50000.10", currency="INR", required_delivery_date=FUTURE)
    pr = client.post("/api/requisitions", json=body, headers={"Idempotency-Key": f"chk-3e-pr-{_n['pr']}"}).json()["id"]
    assert client.post(f"/api/requisitions/{pr}/decision", json={"approver": "mgr.3e", "decision": "APPROVED"}).status_code == 201
    rfq = client.post("/api/rfq/generate", json={"pr_id": pr, "actor": ACTOR}).json()["id"]
    if publish:
        r = client.post(f"/api/rfq/{rfq}/publish", json={"actor": ACTOR, "vendor_ids": [V[i] for i in vendors], "response_deadline": deadline})
        assert r.status_code == 200, r.text
    return rfq


def quote(rfq_id, vendor_idx, qty=100, price="4200.00", tax="18.00", lead=14, c=client):
    body = {"vendor_id": V[vendor_idx], "quoted_quantity": qty, "unit_price": price, "tax_rate_percent": tax,
            "currency": "INR", "lead_time_days": lead}
    r = c.post(f"/api/rfq/{rfq_id}/quotations", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def set_rfq(rfq_id, **values) -> None:
    with SessionLocal() as s:
        s.execute(update(RFQ).where(RFQ.id == rfq_id).values(**values))
        s.commit()


def past_deadline(rfq_id) -> None:
    set_rfq(rfq_id, issue_date=TODAY - timedelta(days=10), response_deadline=TODAY - timedelta(days=1))


def close(rfq_id, actor=ACTOR, c=client, body=None):
    return c.post(f"/api/rfq/{rfq_id}/close", json=body if body is not None else {"actor": actor})


def cancel(rfq_id, reason="Quotes unacceptable", actor=ACTOR, c=client):
    return c.post(f"/api/rfq/{rfq_id}/cancel", json={"actor": actor, "reason": reason})


def closed_rfq(quantity=100, quotes=((1, 100), (2, 60))) -> int:
    rfq = published_rfq(quantity, vendors=sorted({v for v, _q in quotes}))
    for v, q in quotes:
        quote(rfq, v, qty=q)
    past_deadline(rfq)
    r = close(rfq)
    assert r.status_code == 200, r.text
    return rfq


def status_of(rfq_id) -> str:
    with SessionLocal() as s:
        return s.scalar(select(RFQ.status).where(RFQ.id == rfq_id))


def n_audit(rfq_id, action) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.entity_type == "RFQ", AuditEvent.entity_id == str(rfq_id), AuditEvent.action == action))


def audit_row(rfq_id, action):
    with SessionLocal() as s:
        return s.scalars(select(AuditEvent).where(
            AuditEvent.entity_type == "RFQ", AuditEvent.entity_id == str(rfq_id), AuditEvent.action == action)).one()


def quote_rows(rfq_id) -> list:
    with SessionLocal() as s:
        return [(q.vendor_id, q.revision, q.status) for q in s.scalars(select(Quotation).where(Quotation.rfq_id == rfq_id).order_by(Quotation.id))]


def pr_of(rfq_id) -> int:
    with SessionLocal() as s:
        return s.scalar(select(RFQ.pr_id).where(RFQ.id == rfq_id))


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
    original_seal = rules.SEAL_QUOTATIONS_UNTIL_DEADLINE
    original_audit = None
    original_execute = Session.execute
    with SessionLocal() as s:
        category = s.scalar(select(Item.category).where(Item.item_id == "I001"))
        for i, vid in V.items():
            s.add(Vendor(vendor_id=vid, vendor_name=f"Check {vid}", category=category, oem_approved=(i != 5), is_active=True))
        s.commit()
    try:
        print("--- D42: close")
        r = close(999999)
        check("unknown RFQ -> 404 RFQ_NOT_FOUND", r.status_code == 404 and code(r) == "RFQ_NOT_FOUND")
        draft = published_rfq(publish=False)
        r = close(draft)
        check("a DRAFT RFQ -> 409 RFQ_NOT_PUBLISHED (+ status)", r.status_code == 409 and code(r) == "RFQ_NOT_PUBLISHED" and detail_of(r).get("rfq_status") == "DRAFT")
        r1 = published_rfq()
        quote(r1, 1)
        r = close(r1)
        check("before the deadline -> 409 DEADLINE_NOT_PASSED (+ the deadline)", r.status_code == 409 and code(r) == "DEADLINE_NOT_PASSED" and detail_of(r).get("response_deadline") == IN_7)
        set_rfq(r1, issue_date=TODAY, response_deadline=TODAY)
        r = close(r1)
        check("on the deadline day itself -> still 409 (the day is open for quotes)", r.status_code == 409 and code(r) == "DEADLINE_NOT_PASSED")
        check("...no early close: the RFQ is still PUBLISHED", status_of(r1) == "PUBLISHED")
        r2 = published_rfq()
        past_deadline(r2)
        r = close(r2)
        check("deadline passed but NO quotations -> 409 NO_QUOTATIONS", r.status_code == 409 and code(r) == "NO_QUOTATIONS")
        check("...cancel is the way out (PUBLISHED without quotations)", cancel(r2).status_code == 200)

        r3 = published_rfq()
        quote(r3, 1, qty=100)
        quote(r3, 2, qty=60)
        quote(r3, 1, qty=100, price="4100.00")            # V1 revises: 3 rows, only 2 ACTIVE
        past_deadline(r3)
        r = close(r3)
        j = r.json() if r.status_code == 200 else {}
        check("after the deadline with quotations -> 200 CLOSED", r.status_code == 200 and j.get("status") == "CLOSED", f"status={r.status_code}")
        ev = audit_row(r3, "RFQ_CLOSED")
        check("one RFQ_CLOSED audit row; counts ACTIVE quotations (2), not rows (3)", json.loads(ev.after_state)["active_quotations"] == 2 and json.loads(ev.before_state) == {"status": "PUBLISHED"} and len(quote_rows(r3)) == 3)
        with SessionLocal() as s:
            pr_status = s.scalar(select(PurchaseRequisition.status).where(PurchaseRequisition.id == pr_of(r3)))
        check("PR status is unchanged by closing", pr_status == "APPROVED")
        r = close(r3)
        check("close again -> 409 RFQ_ALREADY_CLOSED with rfq_status", r.status_code == 409 and code(r) == "RFQ_ALREADY_CLOSED" and detail_of(r).get("rfq_status") == "CLOSED")
        check("...nothing new was written", n_audit(r3, "RFQ_CLOSED") == 1)
        r = client.post(f"/api/rfq/{r3}/quotations", json={"vendor_id": V[1], "quoted_quantity": 100, "unit_price": "1.00", "tax_rate_percent": "0", "currency": "INR", "lead_time_days": 1})
        check("a CLOSED RFQ accepts no more quotations -> 409 RFQ_NOT_ACCEPTING", r.status_code == 409 and code(r) == "RFQ_NOT_ACCEPTING" and detail_of(r).get("rfq_status") == "CLOSED")
        r = client.post("/api/rfq/generate", json={"pr_id": pr_of(r3), "actor": ACTOR})
        check("a CLOSED RFQ still blocks a new round -> 409 OPEN_RFQ_EXISTS", r.status_code == 409 and code(r) == "OPEN_RFQ_EXISTS" and detail_of(r).get("rfq_status") == "CLOSED")
        cancelled = published_rfq()
        cancel(cancelled)
        r = close(cancelled)
        check("a CANCELLED RFQ cannot be closed -> 409 RFQ_NOT_PUBLISHED", r.status_code == 409 and code(r) == "RFQ_NOT_PUBLISHED" and detail_of(r).get("rfq_status") == "CANCELLED")
        r4 = published_rfq()
        quote(r4, 1)
        past_deadline(r4)
        check("blank actor -> 422", close(r4, body={"actor": "  "}).status_code == 422)
        check("extra field -> 422", close(r4, body={"actor": ACTOR, "status": "CLOSED"}).status_code == 422)
        check("...RFQ still PUBLISHED after the refusals", status_of(r4) == "PUBLISHED")
        res = run_race(10, lambda i, c: close(r4, c=c))
        st = [x.status_code for x in res]
        check("10 simultaneous closes: one 200, nine 409", st.count(200) == 1 and st.count(409) == 9, f"200s={st.count(200)} 409s={st.count(409)} others={[s for s in st if s not in (200, 409)]}")
        check("...refusals say RFQ_ALREADY_CLOSED; one audit row", all(code(x) == "RFQ_ALREADY_CLOSED" for x in res if x.status_code == 409) and n_audit(r4, "RFQ_CLOSED") == 1)

        print("\n--- D43: cancel a CLOSED RFQ (and what still blocks it)")
        c1 = closed_rfq(quantity=100, quotes=((1, 100), (2, 60)))
        before_quotes = quote_rows(c1)
        r = cancel(c1, reason="Both quotes too expensive")
        check("CLOSED with quotations -> cancel 200 CANCELLED", r.status_code == 200 and r.json().get("status") == "CANCELLED", f"status={r.status_code}")
        check("...the quotations are kept as history, unchanged", quote_rows(c1) == before_quotes and all(s == "ACTIVE" for _v, _r, s in before_quotes))
        ev = audit_row(c1, "RFQ_CANCELLED")
        after = json.loads(ev.after_state)
        check("audit: before CLOSED, reason and quotation_count in the after-state", json.loads(ev.before_state) == {"status": "CLOSED"} and after["reason"] == "Both quotes too expensive" and after["quotation_count"] == 2)
        r = client.post("/api/rfq/generate", json={"pr_id": pr_of(c1), "actor": ACTOR})
        check("the PR can be re-tendered: round 2, requested 100 (cancel changes no quantity)", r.status_code == 201 and r.json().get("round_number") == 2 and r.json().get("requested_quantity") == 100)
        p1 = published_rfq()
        quote(p1, 1)
        r = cancel(p1)
        check("PUBLISHED with quotations is still refused -> 409 RFQ_HAS_QUOTATIONS", r.status_code == 409 and code(r) == "RFQ_HAS_QUOTATIONS" and "close" in detail_of(r).get("message", ""))
        for qcs_status, expect_block in (("PENDING_APPROVAL", True), ("APPROVED", True), ("DRAFT", False), ("REJECTED", False)):
            rf = closed_rfq(quotes=((1, 100),))
            with SessionLocal() as s:
                s.add(QCS(rfq_id=rf, status=qcs_status))
                s.commit()
            r = cancel(rf)
            if expect_block:
                check(f"QCS {qcs_status} blocks it -> 409 QCS_IN_PROGRESS (+ qcs_status)", r.status_code == 409 and code(r) == "QCS_IN_PROGRESS" and detail_of(r).get("qcs_status") == qcs_status and status_of(rf) == "CLOSED")
            else:
                check(f"QCS {qcs_status} does NOT block it -> 200", r.status_code == 200 and status_of(rf) == "CANCELLED", f"status={r.status_code}")
        for po_status, expect_block in (("PENDING_APPROVAL", True), ("ERP_PUSHED", True), ("CANCELLED", False), ("REJECTED", False)):
            rf = closed_rfq(quotes=((1, 100),))
            with SessionLocal() as s:
                qid = s.scalar(select(Quotation.id).where(Quotation.rfq_id == rf))
                s.add(new_po(pr_of(rf), qid, V[1], status=po_status))
                s.commit()
            r = cancel(rf)
            if expect_block:
                check(f"an active PO ({po_status}) blocks it -> 409 PO_EXISTS", r.status_code == 409 and code(r) == "PO_EXISTS" and status_of(rf) == "CLOSED")
            else:
                check(f"a {po_status} PO does NOT block it -> 200", r.status_code == 200 and status_of(rf) == "CANCELLED", f"status={r.status_code}")
        r = cancel(c1)
        check("cancel again -> 409 RFQ_ALREADY_CANCELLED", r.status_code == 409 and code(r) == "RFQ_ALREADY_CANCELLED" and detail_of(r).get("rfq_status") == "CANCELLED")
        aw = closed_rfq(quotes=((1, 100),))
        set_rfq(aw, status="AWARDED")
        r = cancel(aw)
        check("an AWARDED RFQ cannot be cancelled -> 409 RFQ_NOT_CANCELLABLE", r.status_code == 409 and code(r) == "RFQ_NOT_CANCELLABLE" and detail_of(r).get("rfq_status") == "AWARDED")
        cr = closed_rfq(quotes=((1, 100),))
        res = run_race(10, lambda i, c: cancel(cr, c=c))
        st = [x.status_code for x in res]
        check("10 simultaneous cancels: one 200, nine 409", st.count(200) == 1 and st.count(409) == 9, f"200s={st.count(200)} 409s={st.count(409)} others={[s for s in st if s not in (200, 409)]}")
        check("...refusals say RFQ_ALREADY_CANCELLED; one audit row", all(code(x) == "RFQ_ALREADY_CANCELLED" for x in res if x.status_code == 409) and n_audit(cr, "RFQ_CANCELLED") == 1)

        print("\n--- the cancel guard is ATOMIC: something arriving between the check and the update")
        sneaky = closed_rfq(quotes=((1, 100),))
        state = {"done": False}

        def hooked(self, statement, *a, **k):
            if not state["done"] and str(statement).startswith("UPDATE rfqs"):
                state["done"] = True
                with SessionLocal() as other:          # a QCS goes to approval at exactly this moment
                    other.add(QCS(rfq_id=sneaky, status="PENDING_APPROVAL"))
                    other.commit()
            return original_execute(self, statement, *a, **k)

        Session.execute = hooked
        try:
            r = cancel(sneaky)
        finally:
            Session.execute = original_execute
        check("a QCS appears just before the update -> 409 RFQ_CHANGED, RFQ still CLOSED", r.status_code == 409 and code(r) == "RFQ_CHANGED" and status_of(sneaky) == "CLOSED" and n_audit(sneaky, "RFQ_CANCELLED") == 0, f"status={r.status_code} code={code(r)}")
        pub_rfq = published_rfq()
        state = {"done": False}

        def hooked2(self, statement, *a, **k):
            if not state["done"] and str(statement).startswith("UPDATE rfqs"):
                state["done"] = True
                with SessionLocal() as other:          # a vendor's quotation lands at exactly this moment
                    other.add(new_quote(pub_rfq, V[1]))
                    other.commit()
            return original_execute(self, statement, *a, **k)

        Session.execute = hooked2
        try:
            r = cancel(pub_rfq)
        finally:
            Session.execute = original_execute
        check("a quotation lands just before the update -> 409 RFQ_CHANGED, still PUBLISHED", r.status_code == 409 and code(r) == "RFQ_CHANGED" and status_of(pub_rfq) == "PUBLISHED", f"status={r.status_code} code={code(r)}")

        print("\n--- D14: audit failure rolls the status change back")
        import app.services.rfqs as svc

        original_audit = svc.record_audit

        def boom(*a, **k):
            raise RuntimeError("audit store down")

        failing = TestClient(app, raise_server_exceptions=False)
        f1 = published_rfq()
        quote(f1, 1)
        past_deadline(f1)
        svc.record_audit = boom
        try:
            r = close(f1, c=failing)
            check("audit fails -> close fails (500), RFQ still PUBLISHED", r.status_code == 500 and status_of(f1) == "PUBLISHED", f"status={r.status_code}")
        finally:
            svc.record_audit = original_audit
        f2 = closed_rfq(quotes=((1, 100),))
        svc.record_audit = boom
        try:
            r = cancel(f2, c=failing)
            check("audit fails -> cancel of a CLOSED RFQ fails (500), still CLOSED", r.status_code == 500 and status_of(f2) == "CLOSED", f"status={r.status_code}")
        finally:
            svc.record_audit = original_audit

        print("\n--- D44/D45: the comparison (facts only)")
        k1 = published_rfq(quantity=100, vendors=(1, 2, 3))
        quote(k1, 1, qty=100, price="4000.00", tax="18.00", lead=14)   # net 4000, incl 4720
        quote(k1, 2, qty=100, price="4200.00", tax="0.00", lead=20)    # net 4200, incl 4200  <- lowest INCLUDING tax
        quote(k1, 3, qty=20, price="4300.00", tax="18.00", lead=7)     # partial 20%, incl 5074
        r = client.get(f"/api/rfq/{k1}/comparison")
        cmp_ = r.json() if r.status_code == 200 else {}
        rows = cmp_.get("rows", [])
        by = {x["vendor_id"]: x for x in rows}
        check("200 with three rows, ordered by vendor id (the order means nothing)", r.status_code == 200 and [x["vendor_id"] for x in rows] == [V[1], V[2], V[3]] and "no meaning" in cmp_.get("ordering", ""), f"status={r.status_code}")
        check("coverage = quoted / requested x 100; 20 of 100 shows 20.00", [x["coverage_percent"] for x in rows] == ["100.00", "100.00", "20.00"], f"{[x['coverage_percent'] for x in rows]}")
        check("full coverage is marked separately; the partial quote is not", [x["full_coverage"] for x in rows] == [True, True, False])
        check("price per unit INCLUDING tax is shown (4 decimals)", [x["unit_price_incl_tax"] for x in rows] == ["4720.0000", "4200.0000", "5074.0000"], f"{[x['unit_price_incl_tax'] for x in rows]}")
        check("lowest marker uses the TAX-INCLUSIVE price: V2 (net 4200) beats V1 (net 4000)", [x["is_lowest_unit_price_incl_tax"] for x in rows] == [False, True, False] and by[V[1]]["unit_price"] == "4000.00" and by[V[2]]["unit_price"] == "4200.00")
        check("shortest lead time is marked (V3, 7 days)", [x["is_shortest_lead_time"] for x in rows] == [False, False, True])
        check("quantities differ: totals NOT comparable, with a note, no 'lowest total'", cmp_.get("totals_comparable") is False and "NOT directly comparable" in (cmp_.get("totals_note") or "") and not any(x["is_lowest_total"] for x in rows))
        check("totals are still shown as strings", [x["total_amount"] for x in rows] == ["472000.00", "420000.00", "101480.00"])
        check("NO score, rank or winner anywhere in the response", not (FORBIDDEN_KEYS & set(cmp_)) and not any(FORBIDDEN_KEYS & set(x) for x in rows))
        check("header facts: item, requested quantity, deadline, count", (cmp_.get("item_id"), cmp_.get("requested_quantity"), cmp_.get("quotation_count"), cmp_.get("response_deadline")) == ("I001", 100, 3, IN_7))
        snapshot = (quote_rows(k1), status_of(k1), n_audit(k1, "RFQ_GENERATED"))
        with SessionLocal() as s:
            audits_before = s.scalar(select(func.count()).select_from(AuditEvent))
        client.get(f"/api/rfq/{k1}/comparison")
        client.get(f"/api/rfq/{k1}/comparison")
        with SessionLocal() as s:
            audits_after = s.scalar(select(func.count()).select_from(AuditEvent))
        check("the comparison is READ-ONLY (quotations, RFQ status and audit rows unchanged)", (quote_rows(k1), status_of(k1), n_audit(k1, "RFQ_GENERATED")) == snapshot and audits_before == audits_after)

        k2 = published_rfq(quantity=100, vendors=(1, 2))
        quote(k2, 1, qty=100, price="4000.00", tax="18.00")
        quote(k2, 2, qty=100, price="4200.00", tax="0.00")
        cmp2 = client.get(f"/api/rfq/{k2}/comparison").json()
        check("same quantities: totals ARE comparable and the lowest total is marked (V2)", cmp2["totals_comparable"] is True and cmp2["totals_note"] is None and [x["is_lowest_total"] for x in cmp2["rows"]] == [False, True])
        k3 = published_rfq(quantity=100, vendors=(1, 2))
        quote(k3, 1, qty=100, price="4000.00", tax="18.00", lead=10)
        quote(k3, 2, qty=100, price="4000.00", tax="18.00", lead=10)
        cmp3 = client.get(f"/api/rfq/{k3}/comparison").json()
        check("ties: every tied vendor is marked", [x["is_lowest_unit_price_incl_tax"] for x in cmp3["rows"]] == [True, True] and [x["is_shortest_lead_time"] for x in cmp3["rows"]] == [True, True] and [x["is_lowest_total"] for x in cmp3["rows"]] == [True, True])
        k4 = published_rfq(quantity=100, vendors=(1,))
        quote(k4, 1)
        cmp4 = client.get(f"/api/rfq/{k4}/comparison").json()
        check("a single quotation: nothing to compare (totals_comparable null), no 'lowest total'", cmp4["totals_comparable"] is None and cmp4["quotation_count"] == 1 and cmp4["rows"][0]["is_lowest_total"] is False)
        k5 = published_rfq(quantity=100, vendors=(1,))
        cmp5 = client.get(f"/api/rfq/{k5}/comparison").json()
        check("no quotations: empty rows, nothing to compare", cmp5["rows"] == [] and cmp5["quotation_count"] == 0 and cmp5["totals_comparable"] is None)
        quote(k2, 1, qty=100, price="3900.00", tax="18.00")
        cmp2 = client.get(f"/api/rfq/{k2}/comparison").json()
        check("only the ACTIVE revision is shown (V1 revision 2, one row per vendor)", [(x["vendor_id"], x["revision"]) for x in cmp2["rows"]] == [(V[1], 2), (V[2], 1)] and cmp2["rows"][0]["unit_price"] == "3900.00")
        k6 = published_rfq(quantity=7, vendors=(1, 2))
        quote(k6, 1, qty=1, price="100.00", tax="18.00")
        quote(k6, 2, qty=3, price="28.25", tax="18.00")                # total 100.01 -> 33.336666... per unit
        cmp6 = client.get(f"/api/rfq/{k6}/comparison").json()
        check("coverage rounds half-up: 1 of 7 -> 14.29, 3 of 7 -> 42.86", [x["coverage_percent"] for x in cmp6["rows"]] == ["14.29", "42.86"], f"{[x['coverage_percent'] for x in cmp6['rows']]}")
        check("unit price incl. tax keeps 4 decimals (100.01 / 3 = 33.3367)", cmp6["rows"][1]["total_amount"] == "100.01" and cmp6["rows"][1]["unit_price_incl_tax"] == "33.3367", f"{cmp6['rows'][1]['unit_price_incl_tax']}")
        k7 = published_rfq(quantity=100, vendors=(5, 1))
        quote(k7, 5)
        cmp7 = client.get(f"/api/rfq/{k7}/comparison").json()
        check("a non-OEM vendor is flagged NON_OEM (a flag, not a block)", cmp7["rows"][0]["vendor_id"] == V[5] and cmp7["rows"][0]["risk_flags"] == ["NON_OEM"] and cmp7["rows"][0]["oem_approved"] is False)
        check("works for a CLOSED RFQ", client.get(f"/api/rfq/{r3}/comparison").json()["rfq_status"] == "CLOSED")
        check("works for a CANCELLED RFQ (quotations stay as history)", client.get(f"/api/rfq/{c1}/comparison").json()["quotation_count"] == 2)
        check("works for a DRAFT RFQ (empty)", client.get(f"/api/rfq/{draft}/comparison").json()["rows"] == [])
        check("unknown RFQ -> 404 RFQ_NOT_FOUND", client.get("/api/rfq/999999/comparison").status_code == 404)

        print("\n--- D47: sealing (the same switch as the quotation list)")
        rules.SEAL_QUOTATIONS_UNTIL_DEADLINE = True
        s1 = published_rfq(quantity=100, vendors=(1,))
        quote(s1, 1)
        r = client.get(f"/api/rfq/{s1}/comparison")
        check("sealed: the comparison is 403 QUOTATIONS_SEALED for everyone (+ the deadline)", r.status_code == 403 and code(r) == "QUOTATIONS_SEALED" and detail_of(r).get("response_deadline") == IN_7 and "everyone" in detail_of(r).get("message", ""))
        past_deadline(s1)
        check("sealed: after the deadline it opens", client.get(f"/api/rfq/{s1}/comparison").status_code == 200)
        s2 = published_rfq(quantity=100, vendors=(1,))
        quote(s2, 1)
        set_rfq(s2, status="CLOSED")
        check("sealed: a CLOSED RFQ is open", client.get(f"/api/rfq/{s2}/comparison").status_code == 200)
        check("sealed: an unknown RFQ is 404, not masked", client.get("/api/rfq/999999/comparison").status_code == 404)
        rules.SEAL_QUOTATIONS_UNTIL_DEADLINE = False
        check("unsealed (the default): readable before the deadline", client.get(f"/api/rfq/{k1}/comparison").status_code == 200)
    finally:
        rules.SEAL_QUOTATIONS_UNTIL_DEADLINE = original_seal
        Session.execute = original_execute
        if original_audit is not None:
            import app.services.rfqs as svc
            svc.record_audit = original_audit
        cleanup(TAG)
        with SessionLocal() as s:
            s.execute(delete(AuditEvent).where(AuditEvent.actor.like("CHK3E%")))
            s.execute(delete(Vendor).where(Vendor.vendor_id.like("CHK3E%")))
            s.commit()

    after_total = total_rows()
    with SessionLocal() as s:
        leftover = s.scalar(select(func.count()).select_from(Vendor).where(Vendor.vendor_id.like("CHK3E%")))
    print(f"\nrows before={before_total} after={after_total}  leftover test vendors={leftover}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total and leftover == 0 else "SOMETHING FAILED")


if __name__ == "__main__":
    main()