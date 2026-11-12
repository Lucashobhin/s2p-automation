"""Prove Phase 3a: POST /api/requisitions (D15-D19) against the real API and database.

Run from backend/ (after seed_vendors and seed_items; needs httpx installed):
    python -m scripts.check_requisition_api

Creates PRs tagged requester 'check.api.3a' and deletes them (and their audit rows) at the end.
"""
import json
import re
import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Item, PurchaseRequisition, Vendor
from scripts._helpers import cleanup, total_rows

TAG = "check.api.3a"
client = TestClient(app)
results: list[bool] = []
FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label:58} {detail}")
    return ok


def body(**kw) -> dict:
    data = dict(requester=TAG, department="Procurement", item_id="I001", quantity=5,
                estimated_budget="50000.10", currency="INR", required_delivery_date=FUTURE)
    data.update(kw)
    return data


def counts() -> tuple[int, int]:
    with SessionLocal() as s:
        prs = s.scalar(select(func.count()).select_from(PurchaseRequisition).where(PurchaseRequisition.requester == TAG))
        aud = s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.actor == TAG, AuditEvent.action == "PR_CREATED"))
    return prs, aud


def post(payload, key=None, corr=None, c=client):
    headers = {}
    if key is not None:
        headers["Idempotency-Key"] = key
    if corr is not None:
        headers["X-Correlation-ID"] = corr
    return c.post("/api/requisitions", json=payload, headers=headers)


def rejects(label: str, expected: int, payload=None, key=None, corr=None, raw=None, why: str = "") -> None:
    """Must be rejected FOR THE RIGHT REASON: `why` has to appear in the error detail
    (otherwise a request that fails for some other reason would pass this test vacuously)."""
    before = counts()
    if raw is not None:
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        r = client.post("/api/requisitions", content=raw, headers=headers)
    else:
        r = post(payload, key, corr)
    detail = json.dumps(r.json().get("detail", "")) if r.headers.get("content-type", "").startswith("application/json") else ""
    ok = r.status_code == expected and why.lower() in detail.lower() and counts() == before
    check(label, ok, f"status={r.status_code} reason_found={why.lower() in detail.lower()} rows_unchanged={counts() == before}")


def main() -> None:
    before_total = total_rows()
    try:
        print("--- D19: create")
        r = post(body(), key="chk-3a-1", corr="wf-3a-1")
        j = r.json() if r.status_code == 201 else {}
        check("valid PR -> 201", r.status_code == 201, f"status={r.status_code}")
        check("fields: PR number, SUBMITTED, ordered 0", bool(re.fullmatch(r"PR-\d{5}", j.get("pr_number", ""))) and j.get("status") == "SUBMITTED" and j.get("ordered_quantity") == 0, f"{j.get('pr_number')} {j.get('status')}")
        check("money is a string, exact", j.get("estimated_budget") == "50000.10", f"estimated_budget={j.get('estimated_budget')!r}")
        pr_id = j.get("id")
        with SessionLocal() as s:
            ev = s.scalars(select(AuditEvent).where(AuditEvent.actor == TAG, AuditEvent.entity_id == str(pr_id))).all()
        ok = len(ev) == 1 and ev[0].action == "PR_CREATED" and ev[0].correlation_id == "wf-3a-1" \
            and ev[0].before_state is None and json.loads(ev[0].after_state)["status"] == "SUBMITTED"
        check("exactly one PR_CREATED audit row, with correlation id", ok, f"rows={len(ev)}")

        print("\n--- D16: idempotency")
        r2 = post(body(), key="chk-3a-1")
        check("same key + same body -> 200, same PR", r2.status_code == 200 and r2.json().get("id") == pr_id, f"status={r2.status_code}")
        check("replay header set", r2.headers.get("Idempotent-Replay") == "true")
        check("replay wrote no new PR and no new audit row", counts() == (1, 1), f"(prs, audits)={counts()}")
        r3 = post(body(requester=f"  {TAG}  ", currency="inr", estimated_budget="50000.1"), key="chk-3a-1")
        check("same key, harmless formatting differences -> 200", r3.status_code == 200 and r3.json().get("id") == pr_id, f"status={r3.status_code}")
        r4 = post(body(quantity=6), key="chk-3a-1")
        check("same key + DIFFERENT body -> 409", r4.status_code == 409, f"status={r4.status_code}")
        check("conflict wrote nothing", counts() == (1, 1), f"{counts()}")
        a, b = post(body()), post(body())
        check("no key: each POST creates a new PR", a.status_code == 201 and b.status_code == 201 and a.json()["id"] != b.json()["id"] and counts() == (3, 3), f"{counts()}")

        print("\n--- D16: 10 simultaneous retries of one new request")
        statuses, ids, lock = [], [], threading.Lock()
        barrier = threading.Barrier(10)

        def worker() -> None:
            c = TestClient(app)
            barrier.wait()
            resp = post(body(quantity=7), key="chk-3a-race", c=c)
            with lock:
                statuses.append(resp.status_code)
                ids.append(resp.json().get("id") if resp.status_code in (200, 201) else None)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        check("exactly one 201, nine 200, one PR id", statuses.count(201) == 1 and statuses.count(200) == 9 and len(set(ids)) == 1, f"201s={statuses.count(201)} 200s={statuses.count(200)} others={[s for s in statuses if s not in (200, 201)]}")
        check("race created ONE new PR and ONE new audit row", counts() == (4, 4), f"{counts()}")

        print("\n--- D16: a replay still works after its delivery date has passed")
        from app.schemas.requisition import PRCreate
        from app.services.requisitions import create_requisition
        old_body = body(required_delivery_date=PAST)
        with SessionLocal() as s:   # the PR was legitimately created "yesterday", when PAST was still valid
            pr_old, created_old = create_requisition(
                s, PRCreate(**old_body), idempotency_key="chk-3a-late", today=date.today() - timedelta(days=1))
            old_id = pr_old.id
        late = post(old_body, key="chk-3a-late")
        check("replay of a now-past-date request -> 200 original", created_old and late.status_code == 200 and late.json().get("id") == old_id, f"status={late.status_code}")
        check("...but a NEW request with that date is refused", post(old_body, key="chk-3a-late-new").status_code == 422)

        print("\n--- D17: validation (each must be 422 and change nothing)")
        rejects("unknown item", 422, body(item_id="NOPE"), why="does not exist")
        rejects("quantity 0", 422, body(quantity=0), why="quantity")
        rejects("quantity true", 422, body(quantity=True), why="quantity")
        rejects("quantity 5.5", 422, body(quantity=5.5), why="quantity")
        rejects("quantity as string", 422, body(quantity="5"), why="quantity")
        rejects("quantity above maximum", 422, body(quantity=1_000_001), why="quantity")
        rejects("quantity absurdly large", 422, body(quantity=10**30), why="quantity")
        rejects("negative budget", 422, body(estimated_budget="-1"), why="estimated_budget")
        rejects("budget with 3 decimals", 422, body(estimated_budget="10.123"), why="estimated_budget")
        rejects("budget NaN", 422, body(estimated_budget="NaN"), why="estimated_budget")
        rejects("budget too many digits", 422, body(estimated_budget="123456789012345"), why="estimated_budget")
        rejects("currency USD (unsupported)", 422, body(currency="USD"), why="unsupported currency")
        rejects("currency 'IN'", 422, body(currency="IN"), why="unsupported currency")
        rejects("delivery date in the past", 422, body(required_delivery_date=PAST), why="today or later")
        rejects("delivery date as a number", 422, body(required_delivery_date=1700000000), why="required_delivery_date")
        rejects("requester only spaces", 422, body(requester="   "), why="requester")
        rejects("department empty", 422, body(department=""), why="department")
        rejects("extra field 'status'", 422, body(status="APPROVED"), why="status")
        rejects("extra field 'ordered_quantity'", 422, body(ordered_quantity=5), why="ordered_quantity")
        rejects("missing field", 422, {k: v for k, v in body().items() if k != "item_id"}, why="item_id")
        rejects("not JSON at all", 422, raw="not json", why="json")
        rejects("blank Idempotency-Key", 422, body(), key="   ", why="Idempotency-Key")
        rejects("Idempotency-Key with a space inside", 422, body(), key="a b", why="Idempotency-Key")
        rejects("Idempotency-Key 65 chars", 422, body(), key="k" * 65, why="Idempotency-Key")
        rejects("X-Correlation-ID 65 chars", 422, body(), corr="c" * 65, why="X-Correlation-ID")
        ok_ws = post(body(requester=f"  {TAG}  ", department="  Procurement  ", item_id=" I001 ")).status_code == 201
        check("surrounding spaces are trimmed, not rejected", ok_ws)

        print("\n--- reads")
        g = client.get(f"/api/requisitions/{pr_id}")
        check("GET by id", g.status_code == 200 and g.json()["pr_number"] == j["pr_number"])
        check("GET unknown id -> 404", client.get("/api/requisitions/999999").status_code == 404)
        lst = client.get("/api/requisitions?limit=200").json()
        check("list contains the new PR", any(p["id"] == pr_id for p in lst), f"returned={len(lst)}")
        check("list status filter", all(p["status"] == "SUBMITTED" for p in client.get("/api/requisitions?status=SUBMITTED").json()))
        check("list limit 0 -> 422", client.get("/api/requisitions?limit=0").status_code == 422)

        print("\n--- D15: routing")
        with SessionLocal() as s:
            n_vendors = s.scalar(select(func.count()).select_from(Vendor))
            n_items = s.scalar(select(func.count()).select_from(Item))
        check("/api/vendors works", len(client.get("/api/vendors").json()) == n_vendors, f"count={n_vendors}")
        check("/api/items works", len(client.get("/api/items").json()) == n_items, f"count={n_items}")
        check("old /vendors and /items are gone", client.get("/vendors").status_code == 404 and client.get("/items").status_code == 404)
        check("/ and /health stay at the root", client.get("/").status_code == 200 and client.get("/health").json()["status"] == "ok")

        print("\n--- D14: audit failure rolls the PR back")
        import app.services.requisitions as svc

        original = svc.record_audit

        def boom(*a, **k):
            raise RuntimeError("audit store down")

        svc.record_audit = boom
        try:
            before = counts()
            resp = post(body(quantity=9), key="chk-3a-atomic", c=TestClient(app, raise_server_exceptions=False))
            check("audit fails -> request fails (500)", resp.status_code == 500, f"status={resp.status_code}")
            check("...and NO PR was left behind", counts() == before, f"{before} -> {counts()}")
        finally:
            svc.record_audit = original
        retry = post(body(quantity=9), key="chk-3a-atomic")
        check("same key works once audit is healthy again", retry.status_code == 201, f"status={retry.status_code}")
    finally:
        cleanup(TAG)

    after_total = total_rows()
    print(f"\nrows before={before_total} after={after_total}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before_total == after_total else "SOMETHING FAILED")


if __name__ == "__main__":
    main()