import asyncio
import logging
import time
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.database import get_session_factory
from app.modules.dialog import escalation_log, escalation_state, history as dialog_history, vk_client
from app.modules.dialog.models import ProcessedEvent
from app.modules.orders import conversation as orders_conversation

logger = logging.getLogger(__name__)

# Резервное хранилище на случай, если DATABASE_URL не настроен — переживёт
# только до перезапуска процесса (см. already_processed / mark_processed).
_fallback_processed_event_ids: set[str] = set()
_MAX_TRACKED_EVENTS = 10_000

# Ссылки на фоновые задачи, чтобы сборщик мусора не убрал их на полпути.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _set_typing_quietly(peer_id: int) -> None:
    try:
        await vk_client.set_typing(peer_id)
    except Exception:
        logger.warning("Failed to set typing indicator for peer_id=%s", peer_id, exc_info=True)


async def already_processed(event_id: str) -> bool:
    # Отметки хранятся в БД, а не в памяти процесса: VK ретраит недоставленные
    # вебхуки, а при рестарте контейнера или масштабировании на второй инстанс
    # in-memory set повтор не ловит.
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return event_id in _fallback_processed_event_ids

    async with session_factory() as session:
        return await session.get(ProcessedEvent, event_id) is not None


async def mark_processed(event_id: str) -> None:
    """Отмечает событие обработанным — ПОСЛЕ того, как ответ клиенту отправлен.

    Раньше отметка ставилась в начале обработки. Если запрос не доживал до
    конца — VK закрывает соединение по своему таймауту, контейнер уходит на
    перезапуск, — отметка всё равно оставалась в базе, и повторную доставку
    от VK опознавало как дубликат и молча выбрасывало. Клиент не получал
    ничего, в логах не оставалось ни строчки.

    Обратная сторона размена: если контейнер умрёт между отправкой ответа и
    этой отметкой, VK повторит доставку и клиент получит ответ дважды. Дубль
    заметен и не страшен, потерянное сообщение незаметно и потому хуже.
    """
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        if len(_fallback_processed_event_ids) >= _MAX_TRACKED_EVENTS:
            _fallback_processed_event_ids.clear()
        _fallback_processed_event_ids.add(event_id)
        return

    async with session_factory() as session:
        session.add(ProcessedEvent(event_id=event_id))
        try:
            await session.commit()
        except IntegrityError:
            # Параллельная доставка того же события успела отметиться первой.
            await session.rollback()


async def handle_message_new(message: dict[str, Any]) -> None:
    peer_id = message["peer_id"]
    text = message.get("text", "")
    if not text:
        return

    started = time.monotonic()

    # Индикатор «печатает» — украшение, ответ клиента от него не зависит.
    # Раньше его ждали до генерации, и он съедал до двух секунд из тех
    # примерно восьми, что VK отводит на ответ вебхуку: на эскалации этого
    # хватало, чтобы не уложиться, VK рвал соединение и клиент не получал
    # ничего. Пусть выполняется сам по себе, параллельно с Claude.
    _fire_and_forget(_set_typing_quietly(peer_id))

    try:
        reply = await orders_conversation.handle_turn(peer_id, text)
    except Exception:
        logger.exception("Claude generation failed for peer_id=%s", peer_id)
        reply = "Извините, сейчас не получается ответить. Мы скоро вернёмся с ответом."

    generated = time.monotonic()
    try:
        await vk_client.send_message(peer_id, reply)
    finally:
        # Хронометраж по стадиям: VK разрывает соединение молча, и без этой
        # строки из логов видно только общую длительность, а не то, что
        # именно не успело. В finally — чтобы замер был и при неудачной
        # отправке, то есть ровно тогда, когда он нужнее всего.
        finished = time.monotonic()
        logger.info(
            "handle_message_new: peer_id=%s ответ=%.2fс отправка=%.2fс всего=%.2fс",
            peer_id,
            generated - started,
            finished - generated,
            finished - started,
        )


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
