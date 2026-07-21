from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger)
    items: Mapped[list] = mapped_column(JSONB)
    items_total: Mapped[float] = mapped_column(Numeric(10, 2))
    delivery_method: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    delivery_cost: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    total: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderDraftRow(Base):
    __tablename__ = "order_drafts"

    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    items: Mapped[list] = mapped_column(JSONB)
    items_total: Mapped[float] = mapped_column(Numeric(10, 2))
    delivery_method: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    delivery_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    delivery_cost: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    stage: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
