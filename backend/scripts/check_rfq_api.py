"""Prove Phase 3c: RFQ generate / publish / cancel (D26-D33 and the three clarifications).

Run from backend/ (after seed_vendors and seed_items; needs httpx installed):
    python -m scripts.check_rfq_api

Creates PRs tagged requester 'check.rfq.3c' and vendors whose ids start with 'CHK3C'; everything,
including audit rows, is deleted at the end.
"""
import json
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Item, PurchaseRequisition, RFQ, RFQVendor, Vendor
from scripts._helpers import cleanup, new_pr, new_quote, new_rfq, total_rows

TAG = "check.rfq.3c"
ACTOR = f"{TAG}.buyer"
client = TestClient(app)
results: list[bool] = []
TODAY = date.today()
FUTURE = (TODAY + timedelta(days=30)).isoformat()
IN_7 = (TODAY + timedelta(days=7)).isoformat()
_n = {"pr": 0}


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label:64} {detail}")
    return ok


def code(r) -> str | None:
    d = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    return d.get("code") if isinstance(d, dict) else None


def detail_of(r) -> dict:
    d = r.json().get("detail")
    return d if isinstance(d, dict) else {}


def approved_pr(quantity: int = 5, delivery: str = FUTURE) -> int:
    _n["pr"] += 1
    body = dict(requester=TAG, department="Procurement", item_id="I001", quantity=quantity,
                estimated_budget="50000.10", currency="INR", required_delivery_date=delivery)
    r = client.post("/api/requisitions", json=body, headers={"Idempotency-Key": f"chk-3c-pr-{_n['pr']}"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    d = client.post(f"/api/requisitions/{pid}/decision", json={"approver": "mgr.3c", "decision": "APPROVED"})
    assert d.status_code == 201, d.text
    return pid


def gen(pr_id, actor=ACTOR, key=None, corr=None, c=client, extra=None, body=None):
    headers = {}
    if key is not None:
        headers["Idempotency-Key"] = key
    if corr is not None:
        headers["X-Correlation-ID"] = corr
    payload = body if body is not None else {"pr_id": pr_id, "actor": actor, **(extra or {})}
    return c.post("/api/rfq/generate", json=payload, headers=headers)


def pub(rfq_id, vendors, deadline=IN_7, actor=ACTOR, c=client, extra=None, body=None, corr=None):
    payload = body if body is not None else {"actor": actor, "vendor_ids": vendors, "response_deadline": deadline, **(extra or {})}
    return c.post(f"/api/rfq/{rfq_id}/publish", json=payload, headers={"X-Correlation-ID": corr} if corr else {})


def cancel(rfq_id, reason="No longer needed", actor=ACTOR, c=client, body=None):
    payload = body if body is not None else {"actor": actor, "reason": reason}
    return c.post(f"/api/rfq/{rfq_id}/cancel", json=payload)


def rfq_status(rfq_id) -> str:
    with SessionLocal() as s:
        return s.scalar(select(RFQ.status).where(RFQ.id == rfq_id))


def n_rfq(pr_id) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(RFQ).where(RFQ.pr_id == pr_id))


def n_invites(rfq_id) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(RFQVendor).where(RFQVendor.rfq_id == rfq_id))


def n_audit(rfq_id, action) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.entity_type == "RFQ", AuditEvent.entity_id == str(rfq_id), AuditEvent.action == action))


def pr_status(pr_id) -> str:
    with SessionLocal() as s:
        return s.scalar(select(PurchaseRequisition.status).where(PurchaseRequisition.id == pr_id))


def set_pr(pr_id, **values) -> None:
    with SessionLocal() as s:
        s.execute(update(PurchaseRequisition).where(PurchaseRequisition.id == pr_id).values(**values))
        s.commit()


def set_rfq(rfq_id, **values) -> None:
    with SessionLocal() as s:
        s.execute(update(RFQ).where(RFQ.id == rfq_id).values(**values))
        s.commit()


def run_race(n: int, fn) -> list:
    out, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker(i: int) -> None:
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
    with SessionLocal() as s:
        category = s.scalar(select(Item.category).where(Item.item_id == "I001"))
        for i in range(1, 6):
            s.add(Vendor(vendor_id=f"CHK3C{i:02d}", vendor_name=f"Check Vendor {i}", category=category, oem_approved=True, is_active=True))
        s.add(Vendor(vendor_id="CHK3CNO", vendor_name="Check NonOEM", category=category, oem_approved=False, is_active=True))
        s.add(Vendor(vendor_id="CHK3CIN", vendor_name="Check Inactive", category=category, oem_approved=True, is_active=False))
        s.add(Vendor(vendor_id="CHK3CXC", vendor_name="Check OtherCategory", category="Zz Other", oem_approved=True, is_active=True))
        s.commit()
    ok_vendors = ["CHK3C01", "CHK3C02"]
    original_audit = None
    try:
        print("--- D27: generate")
        r = gen(999999)
        check("unknown PR -> 404 PR_NOT_FOUND", r.status_code == 404 and code(r) == "PR_NOT_FOUND")
        _n["pr"] += 1
        sub = client.post("/api/requisitions", json=dict(requester=TAG, department="P", item_id="I001", quantity=5, estimated_budget="1", required_delivery_date=FUTURE), headers={"Idempotency-Key": f"chk-3c-pr-{_n['pr']}"}).json()["id"]
        r = gen(sub)
        check("PR still SUBMITTED -> 409 PR_NOT_APPROVED (+ pr_status)", r.status_code == 409 and code(r) == "PR_NOT_APPROVED" and detail_of(r).get("pr_status") == "SUBMITTED")
        p1 = approved_pr(quantity=100)
        r = gen(p1, key="k-3c-1", corr="wf-3c-1")
        j = r.json() if r.status_code == 201 else {}
        check("approved PR -> 201 DRAFT, round 1, requested 100", r.status_code == 201 and j.get("status") == "DRAFT" and j.get("round_number") == 1 and j.get("requested_quantity") == 100 and j.get("vendors") == [], f"status={r.status_code}")
        rfq1 = j.get("id")
        check("RFQ number format", j.get("rfq_number") == f"RFQ-{rfq1:05d}")
        check("one RFQ_GENERATED audit row", n_audit(rfq1, "RFQ_GENERATED") == 1)
        check("PR status is NOT changed by RFQ activity (D30)", pr_status(p1) == "APPROVED")
        r = gen(p1, key="k-3c-other")
        check("second generate while open -> 409 OPEN_RFQ_EXISTS + the open RFQ", r.status_code == 409 and code(r) == "OPEN_RFQ_EXISTS" and detail_of(r).get("rfq_id") == rfq1 and detail_of(r).get("rfq_status") == "DRAFT")
        r = gen(p1, key="k-3c-1")
        check("same key + same body -> 200 replay of the same RFQ", r.status_code == 200 and r.json().get("id") == rfq1 and r.headers.get("Idempotent-Replay") == "true")
        r = gen(p1, actor="someone.else", key="k-3c-1")
        check("same key, different actor -> 409 IDEMPOTENCY_KEY_REUSED", r.status_code == 409 and code(r) == "IDEMPOTENCY_KEY_REUSED")
        check("replay and conflicts wrote nothing", n_rfq(p1) == 1 and n_audit(rfq1, "RFQ_GENERATED") == 1)
        p2 = approved_pr()
        check("SAME key on a DIFFERENT PR is a different request -> 201", gen(p2, key="k-3c-1").status_code == 201)
        for label, kw, why in (
            ("pr_id 0", dict(body={"pr_id": 0, "actor": ACTOR}), "pr_id"),
            ("pr_id as string", dict(body={"pr_id": "5", "actor": ACTOR}), "pr_id"),
            ("pr_id true", dict(body={"pr_id": True, "actor": ACTOR}), "pr_id"),
            ("blank actor", dict(body={"pr_id": p1, "actor": "   "}), "actor"),
            ("extra field", dict(body={"pr_id": p1, "actor": ACTOR, "status": "PUBLISHED"}), "status"),
        ):
            r = gen(p1, **kw)
            check(f"validation: {label} -> 422", r.status_code == 422 and why in json.dumps(r.json()), f"status={r.status_code}")
        check("validation: bad Idempotency-Key -> 422", gen(p1, key="a b").status_code == 422)

        print("\n--- clarification 1: quantity comes from ordered_quantity, cancel changes nothing")
        r = cancel(rfq1)
        check("cancel round 1 (PR qty 100, nothing ordered) -> 200 CANCELLED", r.status_code == 200 and r.json().get("status") == "CANCELLED")
        r = gen(p1)
        j2 = r.json() if r.status_code == 201 else {}
        check("round 2 -> requested_quantity 100 again, round_number 2", r.status_code == 201 and j2.get("requested_quantity") == 100 and j2.get("round_number") == 2, f"{j2.get('requested_quantity')}/{j2.get('round_number')}")
        set_pr(p1, ordered_quantity=60)
        cancel(j2["id"])
        r = gen(p1)
        j3 = r.json() if r.status_code == 201 else {}
        check("60 ordered by POs -> round 3 requested_quantity 40", r.status_code == 201 and j3.get("requested_quantity") == 40 and j3.get("round_number") == 3, f"{j3.get('requested_quantity')}/{j3.get('round_number')}")
        cancel(j3["id"])
        set_pr(p1, ordered_quantity=100)
        r = gen(p1)
        check("everything ordered -> 409 NO_REMAINING_QUANTITY", r.status_code == 409 and code(r) == "NO_REMAINING_QUANTITY")

        print("\n--- clarification 2: the PR's delivery date must still be valid")
        p3 = approved_pr()
        set_pr(p3, required_delivery_date=TODAY - timedelta(days=1))
        r = gen(p3)
        check("generate for an approved PR whose date passed -> 409", r.status_code == 409 and code(r) == "PR_DELIVERY_DATE_PASSED" and n_rfq(p3) == 0)
        p4 = approved_pr()
        d4 = gen(p4).json()["id"]
        set_pr(p4, required_delivery_date=TODAY - timedelta(days=1))
        r = pub(d4, ok_vendors)
        check("publish after the PR's date passed -> 409", r.status_code == 409 and code(r) == "PR_DELIVERY_DATE_PASSED")
        check("...RFQ still DRAFT, no invitations", rfq_status(d4) == "DRAFT" and n_invites(d4) == 0)
        p5 = approved_pr()
        set_pr(p5, required_delivery_date=TODAY)
        d5 = gen(p5).json()["id"]
        r = pub(d5, ok_vendors, deadline=(TODAY + timedelta(days=1)).isoformat())
        check("delivery date is today: no valid deadline exists -> 422", r.status_code == 422 and code(r) == "INVALID_DEADLINE")

        print("\n--- D28: publish")
        p6 = approved_pr(quantity=10)
        d6 = gen(p6).json()["id"]
        r = pub(d6, ok_vendors, corr="wf-3c-6")
        j = r.json() if r.status_code == 200 else {}
        check("publish -> 200 PUBLISHED, issue date today, deadline set", r.status_code == 200 and j.get("status") == "PUBLISHED" and j.get("issue_date") == TODAY.isoformat() and j.get("response_deadline") == IN_7 and j.get("requested_quantity") == 10, f"status={r.status_code}")
        check("both vendors listed, OEM, no risk flags", [(v["vendor_id"], v["oem_approved"], v["risk_flags"]) for v in j.get("vendors", [])] == [("CHK3C01", True, []), ("CHK3C02", True, [])])
        check("2 invitation rows; one RFQ_PUBLISHED audit row", n_invites(d6) == 2 and n_audit(d6, "RFQ_PUBLISHED") == 1)
        with SessionLocal() as s:
            ev = s.scalars(select(AuditEvent).where(AuditEvent.entity_id == str(d6), AuditEvent.action == "RFQ_PUBLISHED")).one()
        after = json.loads(ev.after_state)
        check("audit after-state lists the invited vendors", [v["vendor_id"] for v in after["vendors"]] == ok_vendors and ev.correlation_id == "wf-3c-6" and json.loads(ev.before_state) == {"status": "DRAFT"})
        check("PR status still APPROVED", pr_status(p6) == "APPROVED")
        active = client.get("/api/rfq/active")
        check("GET /api/rfq/active lists it (route not mistaken for an id)", active.status_code == 200 and any(x["id"] == d6 for x in active.json()))
        g = client.get(f"/api/rfq/{d6}")
        check("GET /api/rfq/{id} returns it with vendors", g.status_code == 200 and len(g.json()["vendors"]) == 2)
        r = client.get("/api/rfq/999999")
        check("GET unknown RFQ -> 404 RFQ_NOT_FOUND", r.status_code == 404 and code(r) == "RFQ_NOT_FOUND")
        r = pub(d6, ok_vendors)
        check("publish again -> 409 RFQ_NOT_DRAFT with rfq_status PUBLISHED", r.status_code == 409 and code(r) == "RFQ_NOT_DRAFT" and detail_of(r).get("rfq_status") == "PUBLISHED")
        check("...nothing written", n_invites(d6) == 2 and n_audit(d6, "RFQ_PUBLISHED") == 1)

        print("\n--- D28: publish validation (right code, nothing changes)")
        p7 = approved_pr()
        d7 = gen(p7).json()["id"]

        def bad(label, r, expected_status, why, check_code=None):
            ok = r.status_code == expected_status and why in json.dumps(r.json()) and rfq_status(d7) == "DRAFT" and n_invites(d7) == 0
            if check_code:
                ok = ok and code(r) == check_code
            check(label, ok, f"status={r.status_code} code={code(r)}")

        bad("empty vendor list", pub(d7, []), 422, "vendor_ids")
        bad("21 vendors (limit 20)", pub(d7, [f"X{i:02d}" for i in range(21)]), 422, "vendor_ids")
        bad("duplicate vendor ids", pub(d7, ["CHK3C01", "CHK3C01"]), 422, "CHK3C01", "DUPLICATE_VENDORS")
        r = pub(d7, ["CHK3C01", "NOPE99"])
        bad("unknown vendor", r, 422, "NOPE99", "INVALID_VENDORS")
        check("...reported in `unknown` only", detail_of(r).get("unknown") == ["NOPE99"] and detail_of(r).get("inactive") == [] and detail_of(r).get("wrong_category") == [])
        r = pub(d7, ["CHK3C01", "CHK3CIN"])
        bad("inactive vendor", r, 422, "CHK3CIN", "INVALID_VENDORS")
        check("...reported in `inactive` only", detail_of(r).get("inactive") == ["CHK3CIN"] and detail_of(r).get("unknown") == [])
        r = pub(d7, ["CHK3C01", "CHK3CXC"])
        bad("vendor from another category", r, 422, "CHK3CXC", "INVALID_VENDORS")
        check("...reported in `wrong_category`, with the item category", detail_of(r).get("wrong_category") == ["CHK3CXC"] and detail_of(r).get("item_category") == category)
        r = pub(d7, ["NOPE99", "CHK3CIN", "CHK3CXC"])
        check("all three problems are reported together", r.status_code == 422 and detail_of(r).get("unknown") == ["NOPE99"] and detail_of(r).get("inactive") == ["CHK3CIN"] and detail_of(r).get("wrong_category") == ["CHK3CXC"])
        bad("deadline today", pub(d7, ok_vendors, deadline=TODAY.isoformat()), 422, "INVALID_DEADLINE", "INVALID_DEADLINE")
        bad("deadline in the past", pub(d7, ok_vendors, deadline=(TODAY - timedelta(days=3)).isoformat()), 422, "INVALID_DEADLINE", "INVALID_DEADLINE")
        r = pub(d7, ok_vendors, deadline=(date.fromisoformat(FUTURE) + timedelta(days=1)).isoformat())
        bad("deadline after the PR's delivery date", r, 422, "INVALID_DEADLINE", "INVALID_DEADLINE")
        check("...bounds are returned", detail_of(r).get("earliest") == (TODAY + timedelta(days=1)).isoformat() and detail_of(r).get("latest") == FUTURE)
        bad("deadline as a number", pub(d7, ok_vendors, deadline=1700000000), 422, "response_deadline")
        bad("extra field", pub(d7, ok_vendors, extra={"status": "PUBLISHED"}), 422, "status")
        bad("blank actor", pub(d7, ok_vendors, actor="  "), 422, "actor")
        r = pub(d7, ["CHK3C01", "CHK3CNO"], deadline=FUTURE)
        check("non-OEM vendor is allowed, flagged NON_OEM (A7); deadline = delivery date is OK", r.status_code == 200 and {v["vendor_id"]: v["risk_flags"] for v in r.json()["vendors"]} == {"CHK3C01": [], "CHK3CNO": ["NON_OEM"]}, f"status={r.status_code}")
        p8 = approved_pr()
        d8 = gen(p8).json()["id"]
        check("deadline tomorrow (earliest allowed) is OK", pub(d8, ok_vendors, deadline=(TODAY + timedelta(days=1)).isoformat()).status_code == 200)

        print("\n--- D31 / clarification 3: cancel, with machine-readable conflicts")
        p9 = approved_pr()
        d9 = gen(p9).json()["id"]
        r = cancel(d9, reason="Wrong item")
        check("cancel a DRAFT with a reason -> 200 CANCELLED", r.status_code == 200 and r.json().get("status") == "CANCELLED")
        with SessionLocal() as s:
            ev = s.scalars(select(AuditEvent).where(AuditEvent.entity_id == str(d9), AuditEvent.action == "RFQ_CANCELLED")).one()
        check("audit has the reason and the before status", json.loads(ev.after_state)["reason"] == "Wrong item" and json.loads(ev.before_state) == {"status": "DRAFT"})
        check("PR status still APPROVED; a new RFQ can be generated", pr_status(p9) == "APPROVED" and gen(p9).status_code == 201)
        r = cancel(d9)
        check("cancel again -> 409 RFQ_ALREADY_CANCELLED with rfq_status", r.status_code == 409 and code(r) == "RFQ_ALREADY_CANCELLED" and detail_of(r).get("rfq_status") == "CANCELLED")
        p10 = approved_pr()
        d10 = gen(p10).json()["id"]
        pub(d10, ok_vendors)
        r = cancel(d10)
        check("cancel a PUBLISHED RFQ without quotations -> 200", r.status_code == 200 and r.json().get("status") == "CANCELLED")
        check("...invitations are kept as history", n_invites(d10) == 2)
        p11 = approved_pr()
        d11 = gen(p11).json()["id"]
        pub(d11, ok_vendors)
        with SessionLocal() as s:
            s.add(new_quote(d11, "CHK3C01"))
            s.commit()
        r = cancel(d11)
        check("cancel with a quotation -> 409 RFQ_HAS_QUOTATIONS + count", r.status_code == 409 and code(r) == "RFQ_HAS_QUOTATIONS" and detail_of(r).get("quotation_count") == 1)
        check("...RFQ still PUBLISHED", rfq_status(d11) == "PUBLISHED")
        check("cancel without a reason -> 422", cancel(d11, body={"actor": ACTOR}).status_code == 422)
        check("cancel with a blank reason -> 422", cancel(d11, reason="   ").status_code == 422)
        r = cancel(999999)
        check("cancel unknown RFQ -> 404", r.status_code == 404 and code(r) == "RFQ_NOT_FOUND")
        set_rfq(d11, status="CLOSED")
        r = cancel(d11)
        check("a CLOSED RFQ cannot be cancelled yet -> 409 RFQ_NOT_CANCELLABLE", r.status_code == 409 and code(r) == "RFQ_NOT_CANCELLABLE" and detail_of(r).get("rfq_status") == "CLOSED")
        r = gen(p11)
        check("a CLOSED RFQ still BLOCKS a new round (it is open) -> 409", r.status_code == 409 and code(r) == "OPEN_RFQ_EXISTS" and detail_of(r).get("rfq_status") == "CLOSED")

        print("\n--- clarification 4: the database enforces one open RFQ per PR")
        p12 = approved_pr()
        with SessionLocal() as s:
            s.add(new_rfq(p12, 1, status="DRAFT"))
            s.commit()

        def direct(label, build, expect):
            with SessionLocal() as s:
                try:
                    build(s)
                    s.commit()
                    outcome, ok = "accepted", expect is None
                except IntegrityError as exc:
                    s.rollback()
                    outcome, ok = f"rejected: {str(exc.orig)[:55]}", expect is not None and expect in str(exc.orig)
            check(label, ok, outcome)

        direct("2nd DRAFT RFQ for a PR (bypassing the service) is refused", lambda s: s.add(new_rfq(p12, 2, status="DRAFT")), "UNIQUE")
        direct("a PUBLISHED one is refused too", lambda s: s.add(new_rfq(p12, 3, status="PUBLISHED")), "UNIQUE")
        direct("a CLOSED one is refused too", lambda s: s.add(new_rfq(p12, 4, status="CLOSED")), "UNIQUE")
        direct("CANCELLED never blocks", lambda s: s.add(new_rfq(p12, 5, status="CANCELLED")), None)
        direct("AWARDED never blocks", lambda s: s.add(new_rfq(p12, 6, status="AWARDED")), None)
        direct("requested_quantity 0 is refused", lambda s: s.add(new_rfq(p12, 7, requested_quantity=0)), "ck_rfq_requested_quantity_positive")
        direct("same idempotency key twice on one PR is refused", lambda s: s.add_all([new_rfq(p12, 8, idempotency_key="k-db"), new_rfq(p12, 9, idempotency_key="k-db")]), "UNIQUE")
        direct("same key on a different PR is fine", lambda s: s.add(new_rfq(p2, 20, idempotency_key="k-db")), None)

        print("\n--- races")
        p13 = approved_pr()
        res = run_race(10, lambda i, c: gen(p13, key=f"k-3c-race-{i}", c=c))
        st = [r.status_code for r in res]
        check("10 simultaneous generates: one 201, nine 409", st.count(201) == 1 and st.count(409) == 9, f"201s={st.count(201)} 409s={st.count(409)} others={[s for s in st if s not in (201, 409)]}")
        check("...all nine refusals say OPEN_RFQ_EXISTS", all(code(r) == "OPEN_RFQ_EXISTS" for r in res if r.status_code == 409))
        check("...exactly one RFQ row and one audit row", n_rfq(p13) == 1)
        p14 = approved_pr()
        res = run_race(10, lambda i, c: gen(p14, key="k-3c-same", c=c))
        st = [r.status_code for r in res]
        check("10 retries of ONE request: one 201, nine 200", st.count(201) == 1 and st.count(200) == 9, f"201s={st.count(201)} 200s={st.count(200)} others={[s for s in st if s not in (200, 201)]}")
        check("...one RFQ row", n_rfq(p14) == 1)
        p15 = approved_pr()
        d15 = gen(p15).json()["id"]
        res = run_race(10, lambda i, c: pub(d15, ok_vendors, c=c))
        st = [r.status_code for r in res]
        check("10 simultaneous publishes: one 200, nine 409", st.count(200) == 1 and st.count(409) == 9, f"200s={st.count(200)} 409s={st.count(409)} others={[s for s in st if s not in (200, 409)]}")
        check("...refusals say RFQ_NOT_DRAFT", all(code(r) == "RFQ_NOT_DRAFT" for r in res if r.status_code == 409))
        check("...2 invitations and one RFQ_PUBLISHED audit row", n_invites(d15) == 2 and n_audit(d15, "RFQ_PUBLISHED") == 1)

        print("\n--- D14: audit failure rolls everything back")
        import app.services.rfqs as svc

        original_audit = svc.record_audit

        def boom(*a, **k):
            raise RuntimeError("audit store down")

        failing = TestClient(app, raise_server_exceptions=False)
        p16 = approved_pr()
        svc.record_audit = boom
        try:
            r = gen(p16, key="k-3c-atomic", c=failing)
            check("audit fails -> generate fails (500), no RFQ left", r.status_code == 500 and n_rfq(p16) == 0, f"status={r.status_code}")
        finally:
            svc.record_audit = original_audit
        check("same key works once audit is healthy", gen(p16, key="k-3c-atomic").status_code == 201)
        d16 = gen(approved_pr()).json()["id"]
        svc.record_audit = boom
        try:
            r = pub(d16, ok_vendors, c=failing)
            check("audit fails -> publish fails (500), RFQ still DRAFT, no invitations", r.status_code == 500 and rfq_status(d16) == "DRAFT" and n_invites(d16) == 0, f"status={r.status_code}")
            r = cancel(d16, c=failing)
            check("audit fails -> cancel fails (500), RFQ still DRAFT", r.status_code == 500 and rfq_status(d16) == "DRAFT", f"status={r.status_code}")
        finally:
            svc.record_audit = original_audit
    finally:
        if original_audit is not None:
            import app.services.rfqs as svc
            svc.record_audit = original_audit
        cleanup(TAG)
        with SessionLocal() as s:
            s.execute(delete(Vendor).where(Vendor.vendor_id.like("CHK3C%")))
            s.commit()

    after_total = total_rows()
    with SessionLocal() as s:
        leftover_vendors = s.scalar(select(func.count()).select_from(Vendor).where(Vendor.vendor_id.like("CHK3C%")))
    print(f"\nrows before={before_total} after={after_total}  leftover test vendors={leftover_vendors}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total and leftover_vendors == 0 else "SOMETHING FAILED")


if __name__ == "__main__":
    main()