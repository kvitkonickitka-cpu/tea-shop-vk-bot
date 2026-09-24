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
    cdek_uuid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Номер отправления Ozon. У СДЭКа заказ опознаётся по uuid, у Ozon — по
    # номеру отправления: по нему смотрят статус, печатают этикетку и
    # отменяют.
    ozon_posting: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Платёж в ЮKassa: идентификатор, его статус и статус регистрации чека.
    # Чек регистрирует касса с ОФД, уже после платежа, поэтому статусы два.
    payment_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payment_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    receipt_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderDraftRow(Base):
    __tablename__ = "order_drafts"

    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    items: Mapped[list] = mapped_column(JSONB)
    items_total: Mapped[float] = mapped_column(Numeric(10, 2))
    delivery_method: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    delivery_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    delivery_cost: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    # Получатель, код пункта выдачи и тариф: всё, что нужно СДЭКу и чего нет
    # в остальных колонках. Одним полем, чтобы не заводить их по одной.
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    stage: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
