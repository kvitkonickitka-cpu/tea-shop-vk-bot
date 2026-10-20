"""Фича-флаги в базе: off | allowlist | on, меняются командой в чате Ops.

Переменные окружения для новых клиентских механик не годятся: каждое
переключение — правка `.env.bot` на ВМ и перезапуск, а полное включение
после проверки должно быть делом одной команды. Поэтому флаг — строка
`feature_flags`, а меняет его администратор в чате Ops: `/flag <имя> on`.

Режимы:
- `off` — механики нет ни у кого (и это умолчание: флага нет в таблице);
- `allowlist` — только тестовым аккаунтам (`TEST_VK_IDS`): так новое
  проверяется в бою, не задевая клиентов;
- `on` — всем.

Чтение — из кэша не старше минуты: флаг спрашивается на каждом ходу, и
лишний запрос в базу на каждый вопрос «включено ли» ни к чему. Ошибка базы
— «выключено»: новая механика без флага не должна включаться сама.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, get_session_factory

logger = logging.getLogger(__name__)

OFF, ALLOWLIST, ON = "off", "allowlist", "on"
MODES = (OFF, ALLOWLIST, ON)
CACHE_SECONDS = 60


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, default=OFF)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass(frozen=True)
class Flag:
    name: str
    title: str


# Все флаги, которые понимает код: команда `/flag` принимает только их, чтобы
# опечатка в имени не заводила флаг, который никто не читает. Список — здесь,
# а не в модулях механик: команда должна знать флаг, даже если модуль ещё не
# импортирован.
KNOWN: dict[str, Flag] = {}


def _register(name: str, title: str) -> str:
    KNOWN[name] = Flag(name, title)
    return name


OZON_AUTO_APPROVE = _register("ozon_auto_approve", "Автоподтверждение Ozon после сборки (C8); allowlist — dry-run")


_cache: dict[str, str] = {}
_loaded_at: float = float("-inf")


def forget_cache() -> None:
    global _loaded_at
    _cache.clear()
    _loaded_at = float("-inf")


async def _load() -> dict[str, str]:
    global _cache, _loaded_at
    if time.monotonic() - _loaded_at < CACHE_SECONDS:
        return _cache
    try:
        async with get_session_factory()() as session:
            rows = (await session.execute(select(FeatureFlag.name, FeatureFlag.mode))).all()
    except Exception:
        # Без базы — то, что знали; не знали — всё выключено. Повтор через
        # минуту, а не на каждом вопросе.
        logger.warning("Флаги: не прочитали из базы, работаем на прежних значениях", exc_info=True)
        _loaded_at = time.monotonic()
        return _cache
    _cache = {name: mode for name, mode in rows if mode in MODES}
    _loaded_at = time.monotonic()
    return _cache


async def mode(name: str) -> str:
    return (await _load()).get(name, OFF)


async def enabled_for(name: str, peer_id: int | None) -> bool:
    """Включена ли механика для этого клиента. Без клиента — только режим on."""
    current = await mode(name)
    if current == ON:
        return True
    if current == ALLOWLIST and peer_id is not None:
        from app.modules.analytics import service as analytics

        return peer_id in await analytics.test_peer_ids()
    return False


async def set_mode(name: str, new_mode: str, *, by: str) -> str:
    """Записать режим. Возвращает прежний. Неизвестное имя или режим — ValueError."""
    if name not in KNOWN:
        raise ValueError(f"нет такого флага: {name}")
    if new_mode not in MODES:
        raise ValueError(f"режим — один из: {', '.join(MODES)}")
    previous = await mode(name)
    async with get_session_factory()() as session:
        statement = insert(FeatureFlag).values(name=name, mode=new_mode, updated_by=by[:120])
        await session.execute(statement.on_conflict_do_update(
            index_elements=[FeatureFlag.name],
            set_={"mode": new_mode, "updated_by": by[:120], "updated_at": func.now()},
        ))
        await session.commit()
    forget_cache()
    logger.info("Флаг %s: %s → %s (%s)", name, previous, new_mode, by)
    return previous


async def describe() -> list[dict]:
    """Все известные флаги с режимом, кем и когда переключены."""
    rows: dict[str, FeatureFlag] = {}
    try:
        async with get_session_factory()() as session:
            rows = {row.name: row for row in (await session.execute(select(FeatureFlag))).scalars()}
    except Exception:
        logger.warning("Флаги: не прочитали для списка", exc_info=True)
    result = []
    for name, flag in sorted(KNOWN.items()):
        row = rows.get(name)
        result.append({
            "name": name, "title": flag.title, "mode": row.mode if row else OFF,
            "updated_by": row.updated_by if row else None,
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        })
    return result
