from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.vendor import Vendor
from app.schemas.vendor import VendorCreate, VendorRead

router = APIRouter(prefix="/vendors", tags=["vendors"])


@router.get("", response_model=list[VendorRead])
def list_vendors(
    category: str | None = None,
    oem_approved: bool | None = None,
    is_active: bool | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Vendor)
    if category is not None:
        stmt = stmt.where(Vendor.category == category)
    if oem_approved is not None:
        stmt = stmt.where(Vendor.oem_approved == oem_approved)
    if is_active is not None:
        stmt = stmt.where(Vendor.is_active == is_active)
    return db.scalars(stmt.order_by(Vendor.vendor_id)).all()


@router.get("/{vendor_id}", response_model=VendorRead)
def get_vendor(vendor_id: str, db: Session = Depends(get_db)):
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.post("", response_model=VendorRead, status_code=status.HTTP_201_CREATED)
def create_vendor(payload: VendorCreate, db: Session = Depends(get_db)):
    vendor = Vendor(**payload.model_dump())
    db.add(vendor)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Vendor ID or GST number already exists"
        )
    db.refresh(vendor)
    return vendor
