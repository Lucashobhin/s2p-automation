from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.enums import QCSStatus
from app.db.base import Base
from app.models.purchase_requisition import utcnow

class QCS(Base):
    __tablename__ = "qcs"
    __table_args__ = (
        UniqueConstraint("rfq_id", name="uq_qcs_rfq"),
        ForeignKeyConstraint(
            ["recommended_quotation_id", "rfq_id"],
            ["quotations.id", "quotations.rfq_id"],
            name="fk_qcs_quotation_rfq",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("rfqs.id", name="fk_qcs_rfq_link"), nullable=False)
    recommended_quotation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=QCSStatus.DRAFT.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)