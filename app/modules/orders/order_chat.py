"""Карточка заказа в телеграм-чат заказов.

Отдельным модулем, потому что отправителей стало два. Сверка по таймеру
(`cdek_watch`) шлёт карточку, когда узнает судьбу заявки у СДЭКа — у него
ответ асинхронный, и до проверки неизвестно, состоялся ли заказ вообще. А
для Ozon и для заказов без перевозчика ждать нечего: номер отправления
приходит сразу, и карточка уходит прямо при подтверждении.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.modules.dialog import telegram_client, vk_client
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

DELIVERY_LABELS = {
    "cdek_pvz": "СДЭК, пункт выдачи",
    "cdek_courier": "СДЭК, курьером до адреса",
    "ozon_pvz": "Ozon, пункт выдачи",
    "russian_post": "Почта России",
}


# Статус заказа, о котором в чат уже написали. Сверка по таймеру такие не
# трогает, иначе карточка пришла бы дважды.
STATUS_SENT = "reported"


def chat_id() -> str | None:
    # None означает «чат менеджера по умолчанию»: пока отдельный чат не
    # заведён, сообщения о заказах всё равно должны доходить.
    return settings.telegram_orders_chat_id or None


def card(order: Order, cdek_number: str | None = None) -> str:
    lines = [f"🧾 <b>Заказ №{order.id}</b>"]
    for item in order.items or []:
        total = item.get("price", 0) * item.get("quantity", 1)
        lines.append(f"{item.get('name', 'товар')} × {item.get('quantity', 1)} — {total} руб.")

    delivery = DELIVERY_LABELS.get(order.delivery_method or "", order.delivery_method or "—")
    lines.append(
        f"Товары {order.items_total} руб. + доставка {order.delivery_cost} руб. "
        f"= <b>{order.total} руб.</b>"
    )
    lines.append(f"Доставка: {delivery}")
    if cdek_number:
        lines.append(f"Накладная СДЭК: <code>{cdek_number}</code>")
    if order.ozon_posting:
        lines.append(f"Отправление Ozon: <code>{order.ozon_posting}</code>")
    lines.append(f"Диалог: {vk_client.dialog_link(order.peer_id)}")
    return "\n".join(lines)


async def send(order: Order, text: str) -> bool:
    """Отправить в чат заказов. False — если не дошло."""
    try:
        await telegram_client.send_message(text, chat_id=chat_id())
        return True
    except Exception:
        logger.exception("Не смогли написать в чат заказов про заказ %s", order.id)
        return False
