"""Prove Phase 3b: PR approval (D20-D25) against the real API and database.

Run from backend/ (after seed_vendors and seed_items; needs httpx installed):
    python -m scripts.check_pr_approval_api

Creates PRs tagged requester 'check.appr.3b' and deletes them, their approvals and their
audit rows at the end.
"""
import json
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.core.rules as rules
from app.db.database import SessionLocal
from app.main import app
from app.models import Approval, AuditEvent, PurchaseRequisition
from scripts._helpers import cleanup, total_rows

TAG = "check.appr.3b"
client = TestClient(app)
results: list[bool] = []
FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()
_counter = {"n": 0}


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label:62} {detail}")
    return ok


def pr_body(**kw) -> dict:
    data = dict(requester=TAG, department="Procurement", item_id="I001", quantity=5,
                estimated_budget="50000.10", currency="INR", required_delivery_date=FUTURE)
    data.update(kw)
    return data


def new_pr() -> int:
    _counter["n"] += 1
    r = client.post("/api/requisitions", json=pr_body(), headers={"Idempotency-Key": f"chk-3b-pr-{_counter['n']}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def decide(pr_id, decision, approver="mgr.a", comments=None, key=None, corr=None, c=client, raw=None, extra=None):
    body = {"approver": approver, "decision": decision}
    if comments is not None:
        body["comments"] = comments
    if extra:
        body.update(extra)
    headers = {}
    if key is not None:
        headers["Idempotency-Key"] = key
    if corr is not None:
        headers["X-Correlation-ID"] = corr
    if raw is not None:
        headers["Content-Type"] = "application/json"
        return c.post(f"/api/requisitions/{pr_id}/decision", content=raw, headers=headers)
    return c.post(f"/api/requisitions/{pr_id}/decision", json=body, headers=headers)


def resubmit(pr_id, c=client, **kw):
    return c.post(f"/api/requisitions/{pr_id}/resubmit", json=pr_body(**kw))


def status_of(pr_id) -> str:
    with SessionLocal() as s:
        return s.scalar(select(PurchaseRequisition.status).where(PurchaseRequisition.id == pr_id))


def n_approvals(pr_id) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(Approval).where(Approval.pr_id == pr_id))


def n_audit(pr_id, action) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.entity_type == "PR", AuditEvent.entity_id == str(pr_id), AuditEvent.action == action))


def rejects(label, expected, pr_id, why, **kw) -> None:
    """Must fail with `expected` for the right reason, changing nothing."""
    before = (n_approvals(pr_id), status_of(pr_id))
    r = decide(pr_id, **kw)
    detail = json.dumps(r.json().get("detail", "")) if r.headers.get("content-type", "").startswith("application/json") else ""
    ok = r.status_code == expected and why.lower() in detail.lower() and (n_approvals(pr_id), status_of(pr_id)) == before
    check(label, ok, f"status={r.status_code} reason_found={why.lower() in detail.lower()}")


def main() -> None:
    before_total = total_rows()
    original_levels = rules.required_approval_levels
    try:
        print("--- D22: who may decide")
        check("decision on unknown PR -> 404", decide(999999, "APPROVED").status_code == 404)
        a = new_pr()
        r = decide(a, "APPROVED", approver=f"  {TAG.upper()}  ")
        check("requester approving own PR (case/space-insensitive) -> 403", r.status_code == 403 and "does not prove" in r.text, f"status={r.status_code}")
        check("...and nothing changed", n_approvals(a) == 0 and status_of(a) == "SUBMITTED")

        print("\n--- D20/D21/D25: approve (one level) and idempotency")
        r = decide(a, "APPROVED", comments="Looks good", key="k-3b-a", corr="wf-3b-1")
        j = r.json() if r.status_code == 201 else {}
        check("approve -> 201, level 1, cycle 1, PR APPROVED", r.status_code == 201 and j.get("level") == 1 and j.get("approval_cycle") == 1 and j.get("pr_status") == "APPROVED" and j.get("levels_required") == 1, f"status={r.status_code} {j.get('pr_status')}")
        check("PR is APPROVED in the database, one approval row", status_of(a) == "APPROVED" and n_approvals(a) == 1)
        check("one PR_APPROVED audit row", n_audit(a, "PR_APPROVED") == 1)
        r2 = decide(a, "APPROVED", comments="Looks good", key="k-3b-a")
        check("same key + same body -> 200 replay, same approval", r2.status_code == 200 and r2.json().get("id") == j.get("id") and r2.headers.get("Idempotent-Replay") == "true", f"status={r2.status_code}")
        check("replay wrote nothing", n_approvals(a) == 1 and n_audit(a, "PR_APPROVED") == 1)
        check("same key, equivalent formatting -> 200", decide(a, "approved", approver=" mgr.a ", comments="  Looks good ", key="k-3b-a").status_code == 200)
        r3 = decide(a, "APPROVED", comments="Different words", key="k-3b-a")
        check("same key, CHANGED COMMENTS -> 409 (your example)", r3.status_code == 409, f"status={r3.status_code}")
        check("same key, different decision -> 409", decide(a, "REJECTED", comments="no", key="k-3b-a").status_code == 409)
        check("same key, different approver -> 409", decide(a, "APPROVED", approver="mgr.zed", comments="Looks good", key="k-3b-a").status_code == 409)
        check("conflicts wrote nothing", n_approvals(a) == 1 and status_of(a) == "APPROVED")
        b = new_pr()
        check("SAME key on a DIFFERENT PR is a different request -> 201", decide(b, "APPROVED", comments="Looks good", key="k-3b-a").status_code == 201)

        print("\n--- already decided")
        before_dups = n_audit(a, "APPROVAL_DUPLICATE_ATTEMPT")
        r = decide(a, "APPROVED", approver="mgr.b", key="k-3b-other")
        check("second decision on an APPROVED PR -> 409", r.status_code == 409 and "APPROVED" in r.text, f"status={r.status_code}")
        check("...refused attempt logged as APPROVAL_DUPLICATE_ATTEMPT", n_audit(a, "APPROVAL_DUPLICATE_ATTEMPT") == before_dups + 1)
        check("...and no new approval row", n_approvals(a) == 1)

        print("\n--- validation (422, right reason, nothing changes)")
        c = new_pr()
        rejects("unknown decision value", 422, c, "decision", decision="MAYBE")
        rejects("REJECTED without comments", 422, c, "comments are required", decision="REJECTED")
        rejects("RETURNED with blank comments", 422, c, "comments are required", decision="RETURNED", comments="   ")
        rejects("comments over 1000 characters", 422, c, "comments", decision="APPROVED", comments="x" * 1001)
        rejects("blank approver", 422, c, "approver", decision="APPROVED", approver="   ")
        rejects("client-supplied level is refused", 422, c, "level", decision="APPROVED", extra={"level": 2})
        rejects("client-supplied status is refused", 422, c, "status", decision="APPROVED", extra={"status": "APPROVED"})
        rejects("Idempotency-Key with a space", 422, c, "Idempotency-Key", decision="APPROVED", key="a b")
        rejects("not JSON", 422, c, "json", decision="APPROVED", raw="nope")
        check("lowercase decision is accepted and normalised", decide(c, "approved").status_code == 201)

        print("\n--- reject is terminal")
        d = new_pr()
        r = decide(d, "REJECTED", comments="Over budget", approver="mgr.a")
        check("REJECTED with comment -> 201, PR REJECTED", r.status_code == 201 and r.json().get("pr_status") == "REJECTED" and n_audit(d, "PR_REJECTED") == 1)
        check("decision after rejection -> 409", decide(d, "APPROVED", approver="mgr.b").status_code == 409)
        check("resubmit a REJECTED PR -> 409", resubmit(d).status_code == 409)

        print("\n--- D23/D24: return, resubmit, new cycle")
        e = new_pr()
        r = decide(e, "RETURNED", comments="Quantity looks high", approver="mgr.a")
        check("RETURNED -> 201, cycle 1, PR RETURNED", r.status_code == 201 and r.json().get("pr_status") == "RETURNED" and r.json().get("approval_cycle") == 1 and n_audit(e, "PR_RETURNED") == 1)
        dups = n_audit(e, "APPROVAL_DUPLICATE_ATTEMPT")
        r = decide(e, "APPROVED", approver="mgr.b")
        check("decision while RETURNED -> 409 (needs resubmit)", r.status_code == 409 and "RETURNED" in r.text)
        check("...not logged as a duplicate attempt", n_audit(e, "APPROVAL_DUPLICATE_ATTEMPT") == dups)
        check("resubmit by someone else -> 403", resubmit(e, requester="someone.else").status_code == 403)
        before_state = (n_approvals(e), status_of(e))
        check("resubmit with past date -> 422", resubmit(e, required_delivery_date=PAST).status_code == 422)
        check("resubmit with unknown item -> 422", resubmit(e, item_id="NOPE").status_code == 422)
        check("resubmit with quantity 0 -> 422", resubmit(e, quantity=0).status_code == 422)
        check("...PR still RETURNED, nothing changed", status_of(e) == "RETURNED" and (n_approvals(e), status_of(e)) == before_state)
        r = resubmit(e, quantity=8, department="Engineering")
        check("resubmit by requester with corrections -> 200, SUBMITTED", r.status_code == 200 and r.json()["status"] == "SUBMITTED" and r.json()["quantity"] == 8 and r.json()["department"] == "Engineering", f"status={r.status_code}")
        with SessionLocal() as s:
            ev = s.scalars(select(AuditEvent).where(AuditEvent.entity_id == str(e), AuditEvent.action == "PR_RESUBMITTED")).all()
        ok = len(ev) == 1 and json.loads(ev[0].before_state)["quantity"] == 5 and json.loads(ev[0].after_state)["quantity"] == 8
        check("PR_RESUBMITTED audit row has before and after", ok, f"rows={len(ev)}")
        check("resubmit again (now SUBMITTED) -> 409", resubmit(e).status_code == 409)
        r = decide(e, "APPROVED", approver="mgr.b", comments="Better")
        check("approve after resubmit: approval_cycle 2, level 1, PR APPROVED", r.status_code == 201 and r.json().get("approval_cycle") == 2 and r.json().get("level") == 1 and r.json().get("pr_status") == "APPROVED", f"status={r.status_code} {r.json().get('approval_cycle')}")
        hist = client.get(f"/api/requisitions/{e}/approvals").json()
        check("history keeps both cycles in order", [(h["approval_cycle"], h["decision"]) for h in hist] == [(1, "RETURNED"), (2, "APPROVED")], f"{[(h['approval_cycle'], h['decision']) for h in hist]}")
        check("GET approvals of unknown PR -> 404", client.get("/api/requisitions/999999/approvals").status_code == 404)

        print("\n--- D21: three levels (rules patched for this test)")
        rules.required_approval_levels = lambda pr: 3
        m = new_pr()
        r1 = decide(m, "APPROVED", approver="mgr.l1")
        check("level 1 approved: PR stays SUBMITTED", r1.status_code == 201 and r1.json().get("level") == 1 and r1.json().get("pr_status") == "SUBMITTED" and r1.json().get("levels_required") == 3)
        check("intermediate approval audited as PR_APPROVAL_LEVEL_APPROVED", n_audit(m, "PR_APPROVAL_LEVEL_APPROVED") == 1 and n_audit(m, "PR_APPROVED") == 0)
        r2 = decide(m, "APPROVED", approver="mgr.l2")
        check("level 2 approved: still SUBMITTED", r2.json().get("level") == 2 and r2.json().get("pr_status") == "SUBMITTED")
        r3 = decide(m, "APPROVED", approver="mgr.l3")
        check("level 3 approved: PR APPROVED", r3.json().get("level") == 3 and r3.json().get("pr_status") == "APPROVED" and n_audit(m, "PR_APPROVED") == 1)
        n = new_pr()
        decide(n, "APPROVED", approver="mgr.l1")
        r = decide(n, "RETURNED", approver="mgr.l2", comments="Spec is unclear")
        check("returned at level 2: cycle 1 ends", r.json().get("level") == 2 and r.json().get("approval_cycle") == 1 and status_of(n) == "RETURNED")
        resubmit(n)
        r = decide(n, "APPROVED", approver="mgr.l1")
        check("after resubmit the levels restart: level 1, cycle 2", r.json().get("level") == 1 and r.json().get("approval_cycle") == 2, f"level={r.json().get('level')} cycle={r.json().get('approval_cycle')}")
        decide(n, "APPROVED", approver="mgr.l2")
        r = decide(n, "APPROVED", approver="mgr.l3")
        hist = [(h["approval_cycle"], h["level"], h["decision"]) for h in client.get(f"/api/requisitions/{n}/approvals").json()]
        check("full history is correct", hist == [(1, 1, "APPROVED"), (1, 2, "RETURNED"), (2, 1, "APPROVED"), (2, 2, "APPROVED"), (2, 3, "APPROVED")] and status_of(n) == "APPROVED", f"{hist}")
        o = new_pr()
        decide(o, "APPROVED", approver="mgr.l1")
        r = decide(o, "REJECTED", approver="mgr.l2", comments="No")
        check("rejected at level 2 is terminal", r.json().get("pr_status") == "REJECTED" and decide(o, "APPROVED", approver="mgr.l3").status_code == 409)
        rules.required_approval_levels = original_levels

        print("\n--- races")
        p = new_pr()
        statuses, lock, barrier = [], threading.Lock(), threading.Barrier(10)

        def worker(i: int) -> None:
            cl = TestClient(app)
            barrier.wait()
            resp = decide(p, "APPROVED", approver=f"mgr.race{i}", key=f"k-3b-race-{i}", c=cl)
            with lock:
                statuses.append(resp.status_code)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("10 different approvers at once: one 201, nine 409", statuses.count(201) == 1 and statuses.count(409) == 9, f"201s={statuses.count(201)} 409s={statuses.count(409)} others={[s for s in statuses if s not in (201, 409)]}")
        check("exactly one approval row, PR APPROVED", n_approvals(p) == 1 and status_of(p) == "APPROVED")
        check("one PR_APPROVED audit row, nine refused attempts logged", n_audit(p, "PR_APPROVED") == 1 and n_audit(p, "APPROVAL_DUPLICATE_ATTEMPT") == 9, f"approved={n_audit(p, 'PR_APPROVED')} dups={n_audit(p, 'APPROVAL_DUPLICATE_ATTEMPT')}")
        q = new_pr()
        statuses2, barrier2 = [], threading.Barrier(10)

        def worker2() -> None:
            cl = TestClient(app)
            barrier2.wait()
            resp = decide(q, "APPROVED", comments="ok", key="k-3b-same", c=cl)
            with lock:
                statuses2.append(resp.status_code)

        ts = [threading.Thread(target=worker2) for _ in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("10 retries of ONE request: one 201, nine 200", statuses2.count(201) == 1 and statuses2.count(200) == 9, f"201s={statuses2.count(201)} 200s={statuses2.count(200)} others={[s for s in statuses2 if s not in (200, 201)]}")
        check("one approval row, no refused attempts logged", n_approvals(q) == 1 and n_audit(q, "APPROVAL_DUPLICATE_ATTEMPT") == 0)

        print("\n--- D14: audit failure rolls everything back")
        import app.services.approvals as svc

        original_audit = svc.record_audit

        def boom(*a, **k):
            raise RuntimeError("audit store down")

        f = new_pr()
        svc.record_audit = boom
        try:
            resp = decide(f, "APPROVED", key="k-3b-atomic", c=TestClient(app, raise_server_exceptions=False))
            check("audit fails -> decision fails (500)", resp.status_code == 500, f"status={resp.status_code}")
            check("...no approval row and PR still SUBMITTED", n_approvals(f) == 0 and status_of(f) == "SUBMITTED")
        finally:
            svc.record_audit = original_audit
        check("same key works once audit is healthy", decide(f, "APPROVED", key="k-3b-atomic").status_code == 201)
        g = new_pr()
        decide(g, "RETURNED", comments="fix it")
        svc.record_audit = boom
        try:
            resp = resubmit(g, quantity=9, c=TestClient(app, raise_server_exceptions=False))
            with SessionLocal() as s:
                qty = s.scalar(select(PurchaseRequisition.quantity).where(PurchaseRequisition.id == g))
            check("audit fails -> resubmit fails (500), PR untouched", resp.status_code == 500 and status_of(g) == "RETURNED" and qty == 5, f"status={resp.status_code} qty={qty}")
        finally:
            svc.record_audit = original_audit
    finally:
        rules.required_approval_levels = original_levels
        cleanup(TAG)

    after_total = total_rows()
    print(f"\nrows before={before_total} after={after_total}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total else "SOMETHING FAILED")


if __name__ == "__main__":
    main()