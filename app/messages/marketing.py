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

import re
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert

from app.core import worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages.models import ClientPreference

# Отписка распознаётся кодом, а не моделью: «стоп» должен работать всегда,
# одинаково и без обращения к Claude. Поэтому и правило узкое — короткое
# сообщение без вопроса, где стоп-слово стоит само по себе или в окружении
# вежливых слов. «Стоп, давайте через Ozon» — это правка заказа, её ведёт
# модель; «хватит ли 50 грамм?» — вопрос.
_STOP_START = {"стоп", "хватит"}
_STOP_PHRASES = ("не пишите", "не пиши ", "отпишите", "отписаться", "отпишись", "отпишитесь")
_FILLER = {
    "пожалуйста", "спасибо", "мне", "меня", "уже", "больше", "стоп", "хватит",
    "не", "пишите", "присылать", "писать", "напоминания", "напоминать",
    "рассылку", "рассылки", "сообщения", "всё", "все", "нет", "ну",
}
_MAX_WORDS = 5

# Без базы отписка держится в памяти процесса — лучше, чем забыть её совсем.
_fallback_opted_out: set[int] = set()


def is_stop_request(text: str) -> bool:
    """Просит ли клиент больше не присылать напоминаний."""
    raw = (text or "").strip().lower().replace("ё", "е")
    if not raw or "?" in raw:
        return False
    words = re.findall(r"[а-яa-z]+", raw)
    if not words or len(words) > _MAX_WORDS:
        return False
    phrase = " ".join(words) + " "
    if any(stop in phrase for stop in _STOP_PHRASES):
        return True
    return words[0] in _STOP_START and all(word in _FILLER for word in words[1:])


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
