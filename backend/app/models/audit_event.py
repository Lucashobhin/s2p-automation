from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.purchase_requisition import utcnow


class AuditEvent(Base):
    """Who did what to which record, and when. Deliberately NO foreign key to the entity (D12):
    the trail must outlive deleted records and cover vendors, items and everything else."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)        # user, system or agent name
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)  # e.g. PR_APPROVED
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)   # e.g. PR, QCS, PO, VENDOR
    entity_id: Mapped[str] = mapped_column(String(40), nullable=False)     # text: ids are ints or 'V001'
    before_state: Mapped[str | None] = mapped_column(Text)                 # JSON text snapshot
    after_state: Mapped[str | None] = mapped_column(Text)                  # JSON text snapshot
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)  # n8n workflow / request id
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)