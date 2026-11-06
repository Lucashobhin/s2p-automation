"""Seed 50 SYNTHETIC vendors. Safe to re-run (skips existing IDs).

Run from backend/:  python -m scripts.seed_vendors
All names, GST numbers and scores are fake. Scales: app/core/scales.py (A1).
"""
import random

from sqlalchemy import func, select

from app.db.database import SessionLocal
from app.models.vendor import Vendor
from app.schemas.vendor import VendorCreate

rng = random.Random(42)  # fixed seed = same data every run

ADJ = ["Nimbus", "Kestrel", "Aurion", "Vantor", "Solace",
       "Brindle", "Zephyr", "Quillon", "Marlow", "Tessera"]
NOUN = ["Vertex", "Dynamics", "Circuits", "Systems", "Works",
        "Labs", "Forge", "Nexus", "Source", "Ridge"]
SUFFIX = {
    "Electronic Components": "Electronics Pvt Ltd",
    "PCB Modules": "PCB Solutions",
    "Mechanical Parts": "Engineering Pvt Ltd",
    "Security Hardware": "Security Systems",
}
CATEGORIES = (
    ["Electronic Components"] * 15
    + ["PCB Modules"] * 12
    + ["Mechanical Parts"] * 13
    + ["Security Hardware"] * 10
)
STATES = ["24", "27", "29", "07", "33"]
TERMS = ["Net 30", "Net 45", "Net 60"]

stems = [f"{a} {n}" for a in ADJ for n in NOUN]
rng.shuffle(stems)


def build(i: int) -> VendorCreate:
    category = CATEGORIES[i - 1]
    return VendorCreate(
        vendor_id=f"V{i:03d}",
        vendor_name=f"{stems[i - 1]} {SUFFIX[category]}",
        gst_number=f"{STATES[i % 5]}SYNTH{i:04d}{chr(65 + i % 26)}1Z{chr(65 + (i * 7) % 26)}",
        category=category,
        oem_approved=(i % 4 != 0),   # every 4th vendor is non-OEM
        performance_score=round(rng.uniform(55, 98), 1),
        quality_rating=round(rng.uniform(3.0, 4.9), 1),
        on_time_delivery_rate=round(rng.uniform(70, 99), 1),
        payment_terms=rng.choice(TERMS),
        is_active=(i % 17 != 0),     # V017 and V034 inactive
    )


def main() -> None:
    inserted = skipped = 0
    with SessionLocal() as db:
        for i in range(1, 51):
            data = build(i)  # Pydantic validates ranges here
            if db.get(Vendor, data.vendor_id) is not None:
                skipped += 1
                continue
            db.add(Vendor(**data.model_dump()))
            inserted += 1
        db.commit()

        total = db.scalar(select(func.count()).select_from(Vendor))
        inactive = db.scalar(
            select(func.count()).select_from(Vendor).where(Vendor.is_active.is_(False))
        )
        rows = db.execute(
            select(Vendor.category, Vendor.oem_approved, func.count())
            .group_by(Vendor.category, Vendor.oem_approved)
            .order_by(Vendor.category, Vendor.oem_approved)
        ).all()

    print(f"inserted={inserted} skipped={skipped} total={total} inactive={inactive}")
    for category, oem, n in rows:
        print(f"  {category:22} {'OEM' if oem else 'non-OEM':8} {n}")


if __name__ == "__main__":
    main()
