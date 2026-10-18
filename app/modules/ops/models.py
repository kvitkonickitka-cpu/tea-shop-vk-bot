from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
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
    # error — сбой внешнего сервиса, call — удачный вызов (сколько ждали),
    # turn — ответ клиенту, pulse — минутный пульс с загрузкой ВМ.
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
    # Когда сбой ушёл в Ops-чат подробностью (app/modules/ops/details.py).
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Для пульса — загрузка ВМ и бота за минуту (app/modules/ops/resources.py).
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class LlmUsage(Base):
    """Расход одного запроса к Claude: токены и стоимость в долларах.

    Отдельно от `ops_events`: тот чистится через 30 дней, а расходы нужны и
    за прошлые месяцы. Строка на каждый ответ API — и на повтор запасной
    моделью после отказа тоже: оба запроса оплачены. Персональных данных нет.
    """

    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_at", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # «ход диалога», «ассистент Ops», «пересказ диалога»…
    operation: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # NULL — цены модели нет в `llm_prices.PRICES`: токены есть, стоимости нет.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String, nullable=True)
