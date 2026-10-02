from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OpsEvent(Base):
    """Одно наблюдение: ошибка внешнего сервиса, ответ клиенту или пульс.

    Наблюдения копятся в базе, а не сразу уходят в Monitoring: экземпляров
    контейнера может быть несколько, и p95 по всем сразу честно считается
    только там, где собраны ответы всех. Пульс раз в минуту читает окно отсюда
    и отправляет в Monitoring уже готовые числа.

    Персональных данных здесь нет и быть не должно: только сервис, операция,
    код ответа, длительность и номер заказа.
    """

    __tablename__ = "ops_events"
    __table_args__ = (Index("ix_ops_events_kind_at", "kind", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # error — сбой внешнего сервиса, turn — ответ клиенту, pulse — минутный пульс.
    kind: Mapped[str] = mapped_column(String)
    # yookassa | cdek | ozon | claude — для ошибок.
    api: Mapped[str | None] = mapped_column(String, nullable=True)
    operation: Mapped[str | None] = mapped_column(String, nullable=True)
    # timeout | network | auth | http_4xx | http_5xx | validation | other
    error_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Для ответа клиенту — сколько из duration_ms ушло на Claude.
    llm_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
