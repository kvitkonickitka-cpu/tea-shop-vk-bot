"""Реферальная программа (пакет D): коды, применения, попытки ввода, награды."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReferralCode(Base):
    """Персональный код клиента: ЧАЙ-XXXX-XXXX, один активный на клиента."""

    __tablename__ = "referral_codes"
    __table_args__ = (
        Index("ux_referral_codes_owner_active", "owner_peer_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    owner_peer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_client_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    limit_month: Mapped[int] = mapped_column(Integer)
    limit_total: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReferralRedemption(Base):
    """Применение кода другом: reserved (счёт) → applied (оплата) | voided (истёк, отменён, возврат) | rejected."""

    __tablename__ = "referral_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_id: Mapped[int] = mapped_column(ForeignKey("referral_codes.id"), index=True)
    friend_peer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    friend_client_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    discount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    # Хеши нормализованных данных получателя (HMAC): «одна скидка на друга» и
    # «друг ≠ владелец» без хранения самих значений. Модель их не видит.
    phone_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    email_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    name_point_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    point_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ReferralAttempt(Base):
    """Каждая попытка ввести код — для защиты от перебора (5 неверных в сутки) и разбора."""

    __tablename__ = "referral_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(String)
    ok: Mapped[bool] = mapped_column()
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    via: Mapped[str] = mapped_column(String)  # text | link
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReferralReward(Base):
    """Награда пригласившему (если включена): скидка N % на его следующий заказ."""

    __tablename__ = "referral_rewards"

    id: Mapped[int] = mapped_column(primary_key=True)
    redemption_id: Mapped[int] = mapped_column(ForeignKey("referral_redemptions.id"), unique=True)
    owner_peer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    percent: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="granted")  # granted | used
    used_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
