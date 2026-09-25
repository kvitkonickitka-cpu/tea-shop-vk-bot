from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, func
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
    # Когда клиенту напоминали про неоплаченный счёт. Две отметки, потому
    # что напоминания разные: первое мягкое, второе — «ссылка закроется».
    reminder_1_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_2_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Получатель, код пункта выдачи, тариф — то же, что лежало в черновике.
    # С оплатой отправление заводится уже после платежа, когда черновика
    # нет, и без этой копии заводить его было бы нечем.
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Судьба посылки у перевозчика. Отметки ставятся один раз, первым, кто
    # узнал: опросом перевозчика или ручной командой. По ним же идёт
    # идемпотентность — событие «вручено» не может случиться дважды, а от
    # него зависит закрывающий чек.
    carrier_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    carrier_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    handed_over_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    not_delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OrderPayment(Base):
    """Платёж по заказу — все попытки, а не только последняя.

    Заказу мало одного `payment_id`. Счёт можно выставить повторно (первый
    истёк, банк отказал), и тогда в заказе остаётся последний платёж, а
    уведомление по предыдущему приходить не перестаёт: **отменить pending у
    ЮKassa нельзя, и клиент может заплатить по старой ссылке**. Без этой
    таблицы такое уведомление не находило заказ вовсе — деньги приходили, а
    мы про них не знали.

    Здесь же видно, есть ли по заказу уже успешный платёж: если да, второй
    нужно вернуть, а не проводить.
    """

    __tablename__ = "order_payments"

    payment_id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    # Номер попытки: первый счёт, второй, третий. Из него же выводится ключ
    # идемпотентности, поэтому повторное подтверждение даёт новый платёж, а
    # не возвращает прежний.
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    # Заполняется, когда счёт закрыт с нашей стороны: истёк срок или банк
    # отказал. По ЮKassa он может при этом оставаться оплачиваемым.
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Возврат, если этот платёж пришёл вторым и его пришлось вернуть.
    refund_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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
