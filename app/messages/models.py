from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, String, func
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
