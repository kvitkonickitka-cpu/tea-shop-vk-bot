"""Команды для тестовых аккаунтов: прогон «новым клиентом» на своём аккаунте.

Завести второй аккаунт ВК для проверок не всегда получается, а на своём
владелец давно «постоянный клиент»: на любое первое сообщение бот присылает
«как в прошлый раз», и путь нового клиента — список пунктов, «Нужно быстрее —
СДЭК», «1, ФИО, телефон, почта» — проверить нельзя. Поэтому три команды:

- `/новый` — бот забывает заказы этого аккаунта до текущего момента: нет «как
  в прошлый раз», прошлого получателя, «Покупок клиента», отзыва и повторных
  касаний. Заказы в базе не трогаются — отсекаются по времени
  (`client_preferences.fresh_since`). Заодно — то же, что `/сброс`;
- `/сброс` — черновик, неоплаченные счета и переписка, которую видит модель,
  — с чистого листа; режим «новый» остаётся;
- `/постоянный` — снова обычный режим.

Работают только у аккаунтов из `TEST_VK_IDS`: у остальных это обычные
сообщения, и уходят они модели как есть. Команду разбирает код, модель её не
видит, в историю она не пишется.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.core.database import get_session_factory

logger = logging.getLogger(__name__)

FRESH = "/новый"
RESET = "/сброс"
REGULAR = "/постоянный"
COMMANDS = (FRESH, RESET, REGULAR)


def command_of(text: str | None) -> str | None:
    word = " ".join((text or "").casefold().replace("ё", "е").split())
    for command in COMMANDS:
        if word == command.replace("ё", "е"):
            return command
    return None


async def is_test(peer_id: int) -> bool:
    from app.modules.analytics import service as analytics

    return peer_id in await analytics.test_peer_ids()


async def fresh_since(peer_id: int) -> datetime | None:
    """С какого момента считать заказы этого клиента. None — все.

    Для обычных клиентов — None без запроса к базе: множество тестовых
    аккаунтов уже в памяти.
    """
    try:
        if not await is_test(peer_id):
            return None
        from app.messages.models import ClientPreference

        async with get_session_factory()() as session:
            row = await session.get(ClientPreference, peer_id)
        return row.fresh_since if row is not None else None
    except Exception:
        logger.warning("Не узнали режим «новый клиент» для peer_id=%s", peer_id, exc_info=True)
        return None


def since_clause(column, since: datetime | None):
    """Условие на заказы: только созданные после отметки (или все)."""
    from sqlalchemy import true

    return column >= since if since is not None else true()


async def _set_fresh(peer_id: int, value: datetime | None) -> None:
    from app.messages.models import ClientPreference

    statement = insert(ClientPreference).values(
        peer_id=peer_id, marketing_opt_out=False, fresh_since=value,
    ).on_conflict_do_update(index_elements=[ClientPreference.peer_id], set_={"fresh_since": value})
    async with get_session_factory()() as session:
        await session.execute(statement)
        await session.commit()


async def reset(peer_id: int) -> dict:
    """Черновик, неоплаченные заказы, переписка и отметки рассылок — с чистого листа."""
    from app.messages.models import ClientPreference
    from app.modules.dialog import escalation_state
    from app.modules.dialog.models import ConversationMessage
    from app.modules.orders import cancellation

    outcome = await cancellation.cancel_for_client(peer_id)
    async with get_session_factory()() as session:
        removed = (await session.execute(
            delete(ConversationMessage).where(ConversationMessage.peer_id == peer_id)
        )).rowcount or 0
        preference = await session.get(ClientPreference, peer_id)
        if preference is not None:
            preference.marketing_opt_out = False
            preference.opted_out_at = None
            preference.last_offer_buttons = None
        await session.commit()
    try:
        await escalation_state.mark_resolved(peer_id)
    except Exception:
        logger.warning("Не сняли открытый вопрос при сбросе peer_id=%s", peer_id, exc_info=True)
    return {"canceled": outcome.canceled, "draft": outcome.draft_dropped, "messages": removed}


async def handle(peer_id: int, text: str | None) -> str | None:
    """Ответ на команду — или None, если это не команда тестового аккаунта."""
    command = command_of(text)
    if command is None or not await is_test(peer_id):
        return None
    from app.messages import templates

    if command == REGULAR:
        await _set_fresh(peer_id, None)
        logger.info("Тестовый режим peer_id=%s: обычный (постоянный клиент)", peer_id)
        return templates.TEST_REGULAR
    done = await reset(peer_id)
    if command == FRESH:
        await _set_fresh(peer_id, datetime.now(timezone.utc))
    logger.info("Тестовый режим peer_id=%s: %s, отменено %s, сообщений истории %s",
                peer_id, command, done["canceled"], done["messages"])
    canceled = ", ".join(f"№{n}" for n in done["canceled"])
    return templates.test_reset(fresh=command == FRESH, canceled=canceled)
