from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ClientNotice(Base):
    """Отметка о том, что клиенту уже написали по этому поводу.

    Одно событие — одно сообщение. Без этой отметки любое уведомление,
    которое ЮKassa повторит, или любой тик расписания, увидевший заказ в том
    же состоянии, писали бы клиенту второй раз.

    Ключ составной: `ref` — о чём речь (`order:12`, `escalation:3`), а не
    только номер заказа, потому что писать приходится и по вопросам без
    заказа. Отметка ставится ДО отправки: кто вставил строку, тот и пишет,
    остальные молчат.
    """

    __tablename__ = "client_notices"

    ref: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Пусто — значит попытка была, а сообщение не ушло.
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ManagerNotification(Base):
    """Уведомление менеджеру, которое нельзя потерять.

    Раньше уведомление жило только в попытке отправки: таймаут телеграма в
    две секунды — и вопрос клиента исчезал, хотя ему уже сказали «уточню у
    менеджера». Теперь оно сначала пишется сюда и коммитится, а отправка —
    вторым шагом. Не ушло сразу — уйдёт со следующим тиком расписания.
    """

    __tablename__ = "manager_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Про что уведомление: эскалация, карточка заказа, отказ перевозчика.
    kind: Mapped[str] = mapped_column(String, index=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    peer_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Готовый текст: собирать его заново при повторе значило бы зависеть от
    # данных, которые к тому моменту уже изменились.
    payload: Mapped[str] = mapped_column(String)
    # Чат телеграма. Пусто — чат менеджера по умолчанию.
    chat_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Когда о недоставке сказали администратору в ВК. Резервный канал нужен
    # именно потому, что первый — телеграм: отчёт о недоставленном уходит
    # туда же и при его недоступности тоже не дойдёт.
    fallback_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ClientPreference(Base):
    """Что клиент попросил про рассылки.

    Одна строка на клиента. `marketing_opt_out` гасит только то, что бот пишет
    по своей инициативе ради продажи: «заказ ждёт вас», «повторить заказ?».
    Сообщения по заказам — оплата, доставка, чеки, возвраты — идут всегда:
    это не реклама, а исполнение договора.
    """

    __tablename__ = "client_preferences"

    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    marketing_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class FunnelEvent(Base):
    """Журнал шагов воронки: нажатия кнопок, счета, выставленные кодом.

    Отдельно от `client_notices`: тот — про «одно событие — одно сообщение»
    и уникален по событию, а нажать одну кнопку клиент может и дважды, и
    каждое нажатие нужно посчитать. Здесь только дописываем строки.
    """

    __tablename__ = "funnel_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event: Mapped[str] = mapped_column(String, index=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
