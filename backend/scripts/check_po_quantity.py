"""Prove the B-lite over-ordering guard (A25): ordered_quantity counter + conditional UPDATE.

Run from backend/ (after seed_vendors and seed_items):
    python -m scripts.check_po_quantity

Creates temporary data tagged requester='check.qty.b-lite' and deletes it at the end.
Includes a concurrency test: 10 threads race to order 20 units each against a PR of 100.
"""
import threading

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.enums import ACTIVE_PO_STATUSES, POStatus
from app.db.database import SessionLocal
from app.models import PurchaseOrder, PurchaseRequisition, RFQ, RFQVendor
from app.services.po_quantity import (
    InsufficientRemainingQuantity, POStateError, PRNotFound,
    deactivate_purchase_order, find_quantity_mismatches,
    release_po_quantity, reserve_po_quantity,
)
from scripts._helpers import cleanup, new_po, new_pr, new_quote, new_rfq, total_rows

TAG = "check.qty.b-lite"
VENDORS = [f"V{i:03d}" for i in range(1, 11)]
results: list[bool] = []


def report(label: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {label:50} {detail}")


def counter(pr_id: int) -> int:
    with SessionLocal() as s:
        return s.scalar(select(PurchaseRequisition.ordered_quantity).where(PurchaseRequisition.id == pr_id))


def make_pr_with_quotes(quantity: int, quote_quantities: list[int], rounds: bool = False):
    """PR + RFQ(s) + one quotation per quantity (vendors V001...). Returns (pr_id, [quotation ids])."""
    with SessionLocal() as s:
        pr = new_pr(s, TAG, quantity=quantity)
        qids = []
        rfq = None
        for i, qty in enumerate(quote_quantities):
            if rfq is None or rounds:
                rfq = new_rfq(pr_id=pr.id, round_number=i + 1 if rounds else 1)
                s.add(rfq)
                s.flush()
            s.add(RFQVendor(rfq_id=rfq.id, vendor_id=VENDORS[i]))
            s.flush()
            q = new_quote(rfq.id, VENDORS[i], qty=qty)
            s.add(q)
            s.flush()
            qids.append(q.id)
        s.commit()
        return pr.id, qids


def place_po(pr_id, qid, vendor, qty, status=None) -> int:
    """The B-lite pattern: reserve + insert PO in ONE transaction."""
    with SessionLocal() as s:
        reserve_po_quantity(s, pr_id, qty)
        po = new_po(pr_id, qid, vendor, quantity=qty)
        if status:
            po.status = status
        s.add(po)
        s.commit()
        return po.id


def expect_raises(label, exc_type, fn, after=None) -> None:
    try:
        fn()
        report(label, False, "no error raised")
    except exc_type as e:
        extra = f"{type(e).__name__}"
        ok = after() if after else True
        report(label, ok, extra if ok else f"{extra} but state wrong")
    except Exception as e:
        report(label, False, f"UNEXPECTED {type(e).__name__}: {e}")


def main() -> None:
    before = total_rows()
    try:
        print("--- Database-level guard on the counter")
        with SessionLocal() as s:
            pr = new_pr(s, TAG, quantity=10)
            s.commit()
            pr_id = pr.id
        report("counter starts at 0", counter(pr_id) == 0)

        def raw_set(value):
            with SessionLocal() as s:
                try:
                    s.execute(update(PurchaseRequisition).where(PurchaseRequisition.id == pr_id).values(ordered_quantity=value))
                    s.commit()
                    return "accepted"
                except IntegrityError as e:
                    s.rollback()
                    return str(e.orig)
        msg = raw_set(11)
        report("direct set counter above quantity", "ck_pr_ordered_within_quantity" in msg, msg[:55])
        msg = raw_set(-1)
        report("direct set counter negative", "ck_pr_ordered_nonneg" in msg, msg[:55])

        print("\n--- reserve / release rules")
        with SessionLocal() as s:
            reserve_po_quantity(s, pr_id, 4)
            s.commit()
        report("reserve 4 of 10", counter(pr_id) == 4, f"counter={counter(pr_id)}")

        def over():
            with SessionLocal() as s:
                try:
                    reserve_po_quantity(s, pr_id, 7)
                    s.commit()
                finally:
                    s.rollback()
        expect_raises("reserve 7 when only 6 remain", InsufficientRemainingQuantity, over, lambda: counter(pr_id) == 4)

        def exact():
            with SessionLocal() as s:
                reserve_po_quantity(s, pr_id, 6)
                s.commit()
        exact()
        report("reserve exactly the remaining 6", counter(pr_id) == 10, f"counter={counter(pr_id)}")

        def unknown():
            with SessionLocal() as s:
                reserve_po_quantity(s, 999999, 1)
        expect_raises("reserve on unknown PR", PRNotFound, unknown)

        def zero():
            with SessionLocal() as s:
                reserve_po_quantity(s, pr_id, 0)
        expect_raises("reserve quantity 0", ValueError, zero)

        def too_much_release():
            with SessionLocal() as s:
                release_po_quantity(s, pr_id, 11)
        expect_raises("release more than ordered", Exception, too_much_release, lambda: counter(pr_id) == 10)

        print("\n--- PO + counter in ONE transaction")
        pr2, (qa, qb) = make_pr_with_quotes(50, [10, 10])
        place_po(pr2, qa, VENDORS[0], 10)
        report("first PO places 10", counter(pr2) == 10, f"counter={counter(pr2)}")

        def dup_quote_po():
            with SessionLocal() as s:
                try:
                    reserve_po_quantity(s, pr2, 10)           # counter becomes 20 inside the txn
                    s.add(new_po(pr2, qa, VENDORS[0], quantity=10))  # same quotation: UNIQUE fails
                    s.commit()
                except Exception:
                    s.rollback()
                    raise
        expect_raises("PO insert fails after reserve -> rolled back", IntegrityError, dup_quote_po, lambda: counter(pr2) == 10)

        print("\n--- Cancel / reject releases units")
        po_b = place_po(pr2, qb, VENDORS[1], 10)
        report("second PO places 10 more", counter(pr2) == 20)
        with SessionLocal() as s:
            deactivate_purchase_order(s, po_b, POStatus.CANCELLED.value)
            s.commit()
        report("cancel PO releases 10", counter(pr2) == 10, f"counter={counter(pr2)}")

        def cancel_again():
            with SessionLocal() as s:
                try:
                    deactivate_purchase_order(s, po_b, POStatus.CANCELLED.value)
                    s.commit()
                finally:
                    s.rollback()
        expect_raises("cancel same PO twice", POStateError, cancel_again, lambda: counter(pr2) == 10)

        pr3, (qc,) = make_pr_with_quotes(10, [10])
        po_c = place_po(pr3, qc, VENDORS[0], 10)
        with SessionLocal() as s:
            deactivate_purchase_order(s, po_c, POStatus.REJECTED.value)
            s.commit()
        report("REJECTED PO also releases its units", counter(pr3) == 0, f"counter={counter(pr3)}")

        pr4, (qd,) = make_pr_with_quotes(10, [10])
        po_d = place_po(pr4, qd, VENDORS[0], 10, status=POStatus.ERP_PUSHED.value)

        def cancel_pushed():
            with SessionLocal() as s:
                try:
                    deactivate_purchase_order(s, po_d, POStatus.CANCELLED.value)
                    s.commit()
                finally:
                    s.rollback()
        expect_raises("cancel an ERP_PUSHED PO is blocked", POStateError, cancel_pushed, lambda: counter(pr4) == 10)

        print("\n--- Your example: PR of 100, PO for 60 then PO for 40 (two RFQ rounds)")
        pr5, (q60, q40) = make_pr_with_quotes(100, [60, 40], rounds=True)
        place_po(pr5, q60, VENDORS[0], 60)
        report("PO-A for 60 units", counter(pr5) == 60, f"{counter(pr5)}/100")
        place_po(pr5, q40, VENDORS[1], 40)
        report("PO-B for 40 units", counter(pr5) == 100, f"{counter(pr5)}/100")

        def one_more():
            with SessionLocal() as s:
                try:
                    reserve_po_quantity(s, pr5, 1)
                    s.commit()
                finally:
                    s.rollback()
        expect_raises("one more unit is refused (100/100)", InsufficientRemainingQuantity, one_more, lambda: counter(pr5) == 100)

        print("\n--- Concurrency: 10 threads x 20 units against a PR of 100")
        pr6, qids = make_pr_with_quotes(100, [20] * 10)
        outcomes: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(i: int) -> None:
            barrier.wait()
            try:
                place_po(pr6, qids[i], VENDORS[i], 20)
                res = "ordered"
            except InsufficientRemainingQuantity:
                res = "refused"
            except Exception as e:  # e.g. "database is locked" would show up here
                res = f"ERROR {type(e).__name__}: {e}"
            with lock:
                outcomes.append(res)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ordered, refused = outcomes.count("ordered"), outcomes.count("refused")
        errors = [o for o in outcomes if o.startswith("ERROR")]
        report("exactly 5 ordered, 5 refused, 0 errors", ordered == 5 and refused == 5 and not errors,
               f"ordered={ordered} refused={refused} errors={len(errors)}")
        with SessionLocal() as s:
            po_sum = sum(s.scalars(select(PurchaseOrder.quantity).where(PurchaseOrder.pr_id == pr6, PurchaseOrder.status.in_(ACTIVE_PO_STATUSES))))
        report("counter == sum of active PO quantities == 100", counter(pr6) == po_sum == 100, f"counter={counter(pr6)} po_sum={po_sum}")

        print("\n--- Drift detector")
        with SessionLocal() as s:   # the very first PR only had units reserved by hand (no POs): hand them back
            release_po_quantity(s, pr_id, 10)
            s.commit()
        with SessionLocal() as s:
            tmp = list(s.scalars(select(PurchaseRequisition.id).where(PurchaseRequisition.requester == TAG)))
            clean = find_quantity_mismatches(s, tmp)
        report("no drift after all the operations above", len(clean) == 0, f"mismatches={len(clean)}")
        with SessionLocal() as s:   # simulate a buggy code path that bypassed the service
            s.execute(update(PurchaseRequisition).where(PurchaseRequisition.id == pr3).values(ordered_quantity=5))
            s.commit()
        with SessionLocal() as s:
            drift = find_quantity_mismatches(s, tmp)
        report("detector catches a bypassed counter update", len(drift) == 1 and drift[0].pr_id == pr3,
               f"found={[(d.pr_id, d.counter, d.actual) for d in drift]}")
    finally:
        cleanup(TAG)

    after = total_rows()
    print(f"\nrows before={before} after={after}")
    print(f"{sum(results)}/{len(results)} checks passed")
    print("ALL PASS" if all(results) and before == after else "SOMETHING FAILED")


if __name__ == "__main__":
    main()
