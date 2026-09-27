"""Продающие сообщения по своей инициативе и отписка от них.

Продающими считаются «заказ ждёт вас» (брошенный черновик) и «повторить
заказ?». Их объединяет одно правило: клиент может сказать «стоп», и после
этого они не приходят никогда. Сообщения по заказам — оплата, доставка,
чеки, возвраты — отпиской не гасятся.

Окно отправки уже тихих часов: не позже `marketing_latest_hour` (21:00) и не
раньше `marketing_earliest_hour` (10:00). Предложение купить поздно вечером
раздражает сильнее, чем помогает, а в 09:00 человек ещё не за чаем.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert

from app.core import worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages.models import ClientPreference

# Без базы отписка держится в памяти процесса — лучше, чем забыть её совсем.
_fallback_opted_out: set[int] = set()


def in_window(now: datetime | None = None) -> bool:
    """Можно ли сейчас прислать продающее сообщение."""
    moment = now or worktime.now_msk()
    if worktime.is_quiet(moment):
        return False
    hour = worktime.to_msk(moment).hour
    return settings.marketing_earliest_hour <= hour < settings.marketing_latest_hour


async def is_opted_out(peer_id: int) -> bool:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return peer_id in _fallback_opted_out
    async with session_factory() as session:
        row = await session.get(ClientPreference, peer_id)
    return bool(row and row.marketing_opt_out)


async def opt_out(peer_id: int) -> None:
    """Отписать от продающих сообщений. Повторная отписка ничего не меняет."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        _fallback_opted_out.add(peer_id)
        return
    now = datetime.now(timezone.utc)
    statement = insert(ClientPreference).values(
        peer_id=peer_id, marketing_opt_out=True, opted_out_at=now
    ).on_conflict_do_update(
        index_elements=[ClientPreference.peer_id],
        set_={"marketing_opt_out": True},
    )
    async with session_factory() as session:
        await session.execute(statement)
        await session.commit()
