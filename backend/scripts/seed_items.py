"""Seed 12 SYNTHETIC items (3 per category). Safe to re-run.

Run from backend/:  python -m scripts.seed_items
Categories match the vendor master so vendor discovery can match by category.
"""
from sqlalchemy import func, select

from app.db.database import SessionLocal
from app.models.item import Item
from app.schemas.item import ItemCreate

ITEMS = [
    ("I001", "Security Camera PCB Module", "PCB Modules", "4-layer, 1080p sensor interface", "EA"),
    ("I002", "Access Controller PCB Module", "PCB Modules", "2-layer, RS485 and Wiegand", "EA"),
    ("I003", "Power Supply PCB Module", "PCB Modules", "12V 2A regulated", "EA"),
    ("I004", "Microcontroller 32-bit ARM", "Electronic Components", "QFP-64, 3.3V", "EA"),
    ("I005", "Ceramic Capacitor 100nF", "Electronic Components", "0603, 50V, X7R", "EA"),
    ("I006", "Image Sensor 2MP", "Electronic Components", "1/2.8 inch CMOS", "EA"),
    ("I007", "Aluminium Camera Housing", "Mechanical Parts", "IP66, powder coated", "EA"),
    ("I008", "Mounting Bracket Steel", "Mechanical Parts", "2 mm, zinc plated", "EA"),
    ("I009", "Cable Gland M20", "Mechanical Parts", "Nylon, IP68", "EA"),
    ("I010", "Biometric Fingerprint Reader", "Security Hardware", "Optical, 500 dpi", "EA"),
    ("I011", "Magnetic Door Lock 600 lbs", "Security Hardware", "12/24V DC, with bracket", "EA"),
    ("I012", "RFID Card Reader", "Security Hardware", "125 kHz, Wiegand output", "EA"),
]


def main() -> None:
    inserted = skipped = 0
    with SessionLocal() as db:
        for item_id, name, category, spec, uom in ITEMS:
            data = ItemCreate(
                item_id=item_id, item_name=name, category=category,
                specification=spec, unit_of_measure=uom,
            )
            if db.get(Item, data.item_id) is not None:
                skipped += 1
                continue
            db.add(Item(**data.model_dump()))
            inserted += 1
        db.commit()
        total = db.scalar(select(func.count()).select_from(Item))
    print(f"inserted={inserted} skipped={skipped} total={total}")


if __name__ == "__main__":
    main()
