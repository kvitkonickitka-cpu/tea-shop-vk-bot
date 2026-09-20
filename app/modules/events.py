"""Разбор события ВКонтакте — отдельно от того, как оно к нам попало.

Событие приходит двумя путями: напрямую из вебхука (когда очередь не
настроена) и из очереди по триггеру. Логика в обоих случаях одна, поэтому
живёт здесь, а не в обработчике запроса.
"""

from __future__ import annotations

import logging

from app.modules.dialog import service
from app.modules.orders import service as orders_service

logger = logging.getLogger(__name__)


async def process_event(body: dict) -> None:
    """Обработать событие ВК целиком: дедупликация, разбор, ответ клиенту."""
    event_type = body.get("type")
    event_id = body.get("event_id", "")

    if event_id and await service.already_processed(event_id):
        logger.info("Событие %s уже обработано, пропускаем", event_id)
        return

    if event_type == "message_new":
        message = body.get("object", {}).get("message", {})
        await service.handle_message_new(message)
    elif event_type == "message_reply":
        # У message_new object вложен под ключом "message", у message_reply
        # по документации VK — это сам объект сообщения; на случай если VK
        # пришлёт другой формат, подстрахуемся обоими вариантами.
        reply_object = body.get("object", {})
        message = reply_object.get("message", reply_object)
        await service.handle_message_reply(message)
    elif event_type == "market_order_new":
        await orders_service.handle_new_order(body.get("object", {}))
    else:
        logger.info("Событие типа %s не обрабатываем", event_type)
        return

    # Отмечаем обработанным только здесь, когда ответ клиенту уже ушёл. Если
    # обработка не дошла досюда — упала или её оборвали, — отметки не будет,
    # и повторная доставка отработает заново, а не потеряется как дубликат.
    if event_id:
        await service.mark_processed(event_id)
