import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.database import get_session_factory
from app.modules.dialog import escalation_log, escalation_state, history as dialog_history, vk_client
from app.modules.dialog.models import ProcessedEvent
from app.modules.orders import conversation as orders_conversation

logger = logging.getLogger(__name__)

# Резервное хранилище на случай, если DATABASE_URL не настроен — переживёт
# только до перезапуска процесса (см. is_duplicate).
_fallback_processed_event_ids: set[str] = set()
_MAX_TRACKED_EVENTS = 10_000


async def is_duplicate(event_id: str) -> bool:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        if event_id in _fallback_processed_event_ids:
            return True
        if len(_fallback_processed_event_ids) >= _MAX_TRACKED_EVENTS:
            _fallback_processed_event_ids.clear()
        _fallback_processed_event_ids.add(event_id)
        return False

    # Вставка event_id как первичный ключ в БД вместо in-memory set — VK
    # ретраит недоставленные вебхуки, а при рестарте контейнера или
    # масштабировании на второй инстанс in-memory set не ловит повтор.
    async with session_factory() as session:
        session.add(ProcessedEvent(event_id=event_id))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return True
        return False


async def handle_message_new(message: dict[str, Any]) -> None:
    peer_id = message["peer_id"]
    text = message.get("text", "")
    if not text:
        return

    try:
        await vk_client.set_typing(peer_id)
    except Exception:
        logger.warning("Failed to set typing indicator for peer_id=%s", peer_id, exc_info=True)

    try:
        reply = await orders_conversation.handle_turn(peer_id, text)
    except Exception:
        logger.exception("Claude generation failed for peer_id=%s", peer_id)
        reply = "Извините, сейчас не получается ответить. Мы скоро вернёмся с ответом."

    await vk_client.send_message(peer_id, reply)


async def handle_message_reply(message: dict[str, Any]) -> None:
    # message_reply прилетает и на сообщения, отправленные нашим ботом через
    # API, и на те, что менеджер написал руками в приложении VK. Отличаем их
    # по admin_author_id — он есть только у сообщений живого администратора.
    admin_author_id = message.get("admin_author_id")
    peer_id = message.get("peer_id")

    # Подробный лог сырого объекта — пока не проверяли вживую точное имя
    # поля admin_author_id, это нужно для быстрой диагностики при первом
    # реальном тесте.
    logger.info("message_reply raw object: %s", message)
    logger.info("message_reply: peer_id=%s admin_author_id=%s", peer_id, admin_author_id)

    if not admin_author_id or peer_id is None:
        return

    # Записываем сам текст ответа менеджера в историю переписки — иначе
    # Claude видит только своё старое обещание "уточню и вернусь" и не
    # понимает, что вопрос уже реально закрыт содержательно.
    manager_text = message.get("text", "")
    if manager_text:
        await dialog_history.append_message(peer_id, "assistant", manager_text)

    await escalation_state.mark_resolved(peer_id)
    await escalation_log.resolve_latest(peer_id, admin_author_id)
