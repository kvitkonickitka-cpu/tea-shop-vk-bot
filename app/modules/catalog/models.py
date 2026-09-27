"""Последняя удачная версия каталога из Google Таблицы."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CatalogSnapshot(Base):
    """Одна строка: что бот сейчас продаёт и откуда это взялось.

    В базе, а не только в памяти: контейнеров бывает несколько, и холодный
    старт не должен терять таблицу до ближайшего тика — иначе бот минуты
    продавал бы по старому `catalog.json` из образа.
    """

    __tablename__ = "catalog_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    items: Mapped[list] = mapped_column(JSON, default=list)
    # Отпечаток CSV: одинаковая таблица не переписывается на каждом тике.
    source_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Когда таблица последний раз поменялась и когда её последний раз читали.
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Ошибка последнего чтения. Менеджеру о ней говорим один раз на каждую
    # новую ошибку, а не каждые пять минут, пока таблицу не поправили.
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_error_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
