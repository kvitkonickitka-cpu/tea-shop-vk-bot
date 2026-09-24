"""Отправка клиенту того, что бот решил сказать сам.

Три правила, которые здесь и живут.

**Одно событие — одно сообщение.** Отметка в `client_notices` ставится до
отправки, и кто её поставил, тот и пишет. ЮKassa повторяет уведомление,
пока не получит 200, а тик расписания видит заказ каждые пять минут — без
отметки клиент получал бы одно и то же по кругу.

**Клиент видел — модель знает.** Каждое отправленное сообщение попадает в
историю диалога как реплика бота. Иначе на следующем ходу модель не в
курсе, что клиенту уже написали про отмену счёта, и здоровается заново.

**Отказ ВК — не повод падать.** Клиент мог запретить сообщения сообществу;
повторять такое бесполезно. Отмечаем попытку, говорим менеджеру и идём
дальше: вызывающий работает по уведомлению или по таймеру, и его работа от
этого не отменяется.
"""

from __future__ import annotations

import logging
import zlib

from sqlalchemy.dialects.postgresql import insert

from app.core.database import get_session_factory
from app.messages.models import ClientNotice
from app.modules.dialog import history as dialog_history, vk_client

logger = logging.getLogger(__name__)

# Отметки на случай работы без базы: переживут только до перезапуска, но и
# в этом режиме клиент не получит одно и то же дважды за жизнь процесса.
_fallback_sent: set[tuple[str, str]] = set()


def order_ref(order_id) -> str:
    return f"order:{order_id}"


def escalation_ref(escalation_id) -> str:
    return f"escalation:{escalation_id}"


def random_id(ref: str, event_type: str) -> int:
    """Идентификатор сообщения для ВК, выведенный из события.

    ВК отбрасывает повторную отправку с тем же `random_id`, поэтому он не
    случайный: если наша отметка почему-то не сработала, дубликат срежет уже
    сам ВК. Влезаем в signed int32 — больше он не принимает.
    """
    return zlib.crc32(f"{ref}:{event_type}".encode("utf-8")) & 0x7FFFFFFF


async def _claim(ref: str, event_type: str, peer_id: int) -> bool:
    """Занять событие. False — про это уже писали (или пишут прямо сейчас)."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        key = (ref, event_type)
        if key in _fallback_sent:
            return False
        _fallback_sent.add(key)
        logger.warning(
            "База недоступна: отметку об отправке клиенту держим в памяти (%s %s)",
            ref, event_type,
        )
        return True

    statement = (
        insert(ClientNotice)
        .values(ref=ref, event_type=event_type, peer_id=peer_id, attempts=0)
        .on_conflict_do_nothing(index_elements=[ClientNotice.ref, ClientNotice.event_type])
        .returning(ClientNotice.ref)
    )
    async with session_factory() as session:
        claimed = (await session.execute(statement)).scalars().first()
        await session.commit()
    return claimed is not None


async def _mark(ref: str, event_type: str, *, sent: bool, error: str = "") -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return

    from datetime import datetime, timezone

    async with session_factory() as session:
        row = await session.get(ClientNotice, (ref, event_type))
        if row is None:
            return
        row.attempts = (row.attempts or 0) + 1
        if sent:
            row.sent_at = datetime.now(timezone.utc)
            row.last_error = None
        else:
            row.last_error = error[:500]
        await session.commit()


async def send(
    *,
    peer_id: int,
    ref: str,
    event_type: str,
    text: str,
    on_failure=None,
) -> bool:
    """Сказать клиенту один раз. True — сообщение ушло.

    `on_failure` вызывается с текстом ошибки, когда ВК отказал: так
    вызывающий сообщает менеджеру, что клиент новость не получил.
    """
    if not await _claim(ref, event_type, peer_id):
        logger.info("Про «%s» по %s клиенту уже писали", event_type, ref)
        return False

    try:
        await vk_client.send_message(peer_id, text, random_id=random_id(ref, event_type))
    except Exception as error:
        logger.exception("Не отправили клиенту «%s» по %s", event_type, ref)
        await _mark(ref, event_type, sent=False, error=f"{type(error).__name__}: {error}")
        if on_failure is not None:
            await on_failure(f"{type(error).__name__}: {str(error)[:300]}")
        return False

    await _mark(ref, event_type, sent=True)

    # В историю пишем только отправленное: иначе модель будет считать, что
    # клиент прочитал то, чего не получил.
    try:
        await dialog_history.append_message(
            peer_id, "assistant", text, author=dialog_history.AUTHOR_BOT
        )
    except Exception:
        logger.exception("Не записали в историю сообщение клиенту по %s", ref)

    return True


async def already_sent(ref: str, event_type: str) -> bool:
    """Писали ли уже про это событие — без попытки занять его."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return (ref, event_type) in _fallback_sent

    async with session_factory() as session:
        row = await session.get(ClientNotice, (ref, event_type))
    return row is not None
