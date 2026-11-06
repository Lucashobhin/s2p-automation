from sqlalchemy import Boolean, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Vendor(Base):
    __tablename__ = "vendors"

    vendor_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    vendor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    gst_number: Mapped[str | None] = mapped_column(String(20), unique=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    oem_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    performance_score: Mapped[float | None] = mapped_column(Float)
    quality_rating: Mapped[float | None] = mapped_column(Float)
    on_time_delivery_rate: Mapped[float | None] = mapped_column(Float)
    payment_terms: Mapped[str | None] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
