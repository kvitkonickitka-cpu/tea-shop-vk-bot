"""Контроль ответа на вопрос, переданный менеджеру.

Бот сказал клиенту «уточню и вернусь» — и на этом всё заканчивалось: если
менеджер не ответил, никто об этом не узнавал. Вопрос лежал открытым, клиент
ждал, а единственным следом оставалась строка в таблице.

Здесь считаются **рабочие** минуты, а не календарные: вопрос, заданный в
23:40, ждёт ответа не всю ночь, а с открытия. Когда их набралось больше
порога, менеджеру уходит напоминание, а клиенту — одно сообщение, что вопрос
всё ещё у человека. И то и другое по разу на вопрос.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core import worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import client as client_messages, manager as manager_messages, templates
from app.modules.dialog import vk_client
from app.modules.dialog.models import Escalation

logger = logging.getLogger(__name__)

# Сколько вопросов разбираем за тик: их много не бывает, а тик не резиновый.
_BATCH = 20


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


async def check_open_questions() -> dict:
    """Напомнить о вопросах, которые ждут ответа слишком долго."""
    result = {"checked": 0, "reping": 0, "client_told": 0}

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        result["skipped"] = "база недоступна"
        return result

    now = datetime.now(timezone.utc)
    threshold = settings.escalation_reping_after_working_minutes

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Escalation)
                .where(
                    Escalation.resolved_at.is_(None),
                    Escalation.reping_sent_at.is_(None),
                )
                .order_by(Escalation.created_at)
                .limit(_BATCH)
            )
        ).scalars().all()
        pending = [
            (row.id, row.peer_id, row.question, row.reason, _aware(row.created_at))
            for row in rows
        ]

    for escalation_id, peer_id, question, reason, created_at in pending:
        result["checked"] += 1
        waited = worktime.working_minutes_between(created_at, now)
        if waited < threshold:
            continue

        # Менеджеру — напоминание со ссылкой на диалог: отвечать он будет
        # там же, а не в телеграме.
        await manager_messages.notify(
            manager_messages.ESCALATION_REPING,
            templates.manager_escalation_reping(
                question, reason, waited, vk_client.dialog_link(peer_id)
            ),
            peer_id=peer_id,
            chat_id=None,
        )
        result["reping"] += 1

        told = False
        if settings.escalation_client_ping_enabled:
            told = await client_messages.send(
                peer_id=peer_id,
                ref=client_messages.escalation_ref(escalation_id),
                event_type=templates.ESCALATION_WAITING,
                text=templates.escalation_waiting(),
            )
            if told:
                result["client_told"] += 1

        await _mark(escalation_id, told=told)

    return result


async def _mark(escalation_id: int, *, told: bool) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        row = await session.get(Escalation, escalation_id)
        if row is None:
            return
        row.reping_sent_at = now
        if told:
            row.client_ping_sent_at = now
        await session.commit()
