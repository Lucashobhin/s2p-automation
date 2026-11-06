from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Item(Base):
    __tablename__ = "items"

    item_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    item_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    specification: Mapped[str | None] = mapped_column(String(500))
    unit_of_measure: Mapped[str] = mapped_column(String(20), nullable=False)
