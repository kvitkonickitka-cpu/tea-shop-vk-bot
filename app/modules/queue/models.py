from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class QueuedEvent(Base):
    """Событие, которое ждёт разбора (app/modules/queue/local.py).

    Секрета сообщества здесь нет — его проверяет вебхук и отрезает, как
    отрезал для Yandex-очереди. Текст сообщения клиента лежит здесь так же,
    как в `inbound_messages`, и удаляется через сутки после разбора.
    """

    __tablename__ = "queued_events"
    __table_args__ = (
        Index("ix_queued_events_ready", "available_at", postgresql_where=text("done_at is null")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Раньше этого не брать: пауза перед повтором после сбоя.
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Взято в работу. Взятое и не законченное за _LEASE_SECONDS — снова свободно:
    # обработчик умер вместе с контейнером.
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Только класс ошибки: текст исключения может нести данные клиента.
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
