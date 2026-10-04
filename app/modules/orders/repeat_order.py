"""«Повторить заказ» одним нажатием — и тот же путь, если клиент ответил словами.

Кнопка «Повторить» под напоминанием — явное намерение, поэтому счёт
выставляется сразу: состав прошлого заказа по текущим ценам и наличию,
прошлый пункт проверен и посчитан заново, прошлый получатель с проверенной
почтой, порог бесплатной доставки. Всё это клиент видит в сообщении со
ссылкой — включая новую цену, если она изменилась.

Не сошлось (товара нет, пункт закрыт, почта не проходит) — счёт не
выставляем: черновик остаётся, модель получает описание того, что не
сошлось, и продолжает обычным путём.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.messages import funnel
from app.modules.catalog import service as catalog_service
from app.modules.orders import offers, repeat_delivery, repository as orders_repository, state
from app.modules.orders.repeat_delivery import LastRecipient
from app.modules.orders.state import OrderDraft

logger = logging.getLogger(__name__)

TOOL = {
    "name": "repeat_order",
    "description": (
        "Повторить прошлый заказ клиента целиком: тот же состав по текущим "
        "ценам, та же доставка и тот же получатель — инструмент сам всё "
        "проверит и пришлёт счёт. Вызывай, когда клиент хочет повторить "
        "прошлый заказ («да, давайте так же» на «повторить заказ?»). Если "
        "клиент хочет что-то другое — не вызывай, работай как обычно."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "integer", "description": "Номер заказа, который повторяем"},
        },
        "required": ["order_id"],
    },
}


async def repeatable_order(peer_id: int):
    """Последний удачный заказ клиента — тот, что предлагаем повторить."""
    orders = await repeat_delivery._successful_orders(peer_id)
    return orders[0] if orders else None


async def repeat_order(peer_id: int, source_order_id: int):
    """Повторить заказ: счёт, если всё сошлось, иначе пояснение для модели."""
    from app.modules.orders import conversation
    from app.modules.orders.conversation import ToolExecution

    order = await orders_repository.by_id(int(source_order_id))
    if (
        order is None or order.peer_id != peer_id
        or order.payment_status != orders_repository.PAID
        or order.status in ("refunded", orders_repository.CANCELED)
    ):
        return ToolExecution(
            f"Заказа №{source_order_id} для повтора у клиента нет. Уточни, что он хочет заказать."
        )

    catalog = catalog_service.load_items()
    items, missing, repriced = [], [], []
    for row in order.items or []:
        match = catalog_service.find_item(str(row.get("name", "")), catalog)
        if not match or not match.get("in_stock", True):
            missing.append(row.get("name", "товар"))
            continue
        if float(match["price"]) != float(row.get("price") or 0):
            repriced.append(f"{match['name']}: было {row.get('price')} ₽, теперь {match['price']} ₽")
        items.append({"name": match["name"], "quantity": int(row.get("quantity") or 1), "price": match["price"]})

    draft = OrderDraft(
        items=items,
        items_total=sum(float(i["price"]) * i["quantity"] for i in items),
        stage="awaiting_delivery",
        details={"repeat_of": order.id},
    )
    details = order.details or {}
    delivery = repeat_delivery._from_order(order)
    recipient = None
    if details.get("recipient_name") and details.get("recipient_phone"):
        recipient = LastRecipient(
            order.id, details["recipient_name"], details["recipient_phone"],
            details.get("recipient_email") or "",
        )
    offer = await offers.prepare(draft, delivery, recipient) if items else None

    problems = []
    if missing:
        problems.append(f"нет в наличии: {', '.join(missing)}")
    if offer is not None:
        problems.extend(offer.problems)
    if not items or problems or offer is None or not offer.ready:
        if not items:
            return ToolExecution(
                f"Повторить заказ №{order.id} нельзя: из него сейчас ничего нет в наличии "
                f"({', '.join(missing)}). Скажи клиенту и предложи похожее из ассортимента."
            )
        if delivery is not None and offer is not None and offer.point_ok:
            delivery.remember(draft.details)
        note = (
            f"Повтор заказа №{order.id}: черновик создан — "
            + ", ".join(f"{i['name']} × {i['quantity']}" for i in items)
            + f". Счёт не выставлен, потому что {'; '.join(problems)}. Скажи клиенту, что "
            "не сошлось, и продолжай оформление обычным путём."
        )
        if repriced:
            note += " Цены изменились: " + "; ".join(repriced) + "."
        if delivery is not None and offer is not None and offer.point_ok:
            note += " Прошлый пункт доступен: " + repeat_delivery.suggestion(delivery)
        if recipient is not None and offer is not None and offer.recipient_ok:
            note += " " + repeat_delivery.recipient_suggestion(recipient)
        draft.details["repeat_note"] = note
        draft.details["origin"] = "repeat"
        await state.set_draft(peer_id, draft)
        await funnel.record(peer_id, "draft_created", origin="repeat", repeat_of=order.id)
        return ToolExecution(note)

    offers.apply(draft, offer)
    draft.details["origin"] = "repeat"
    await state.set_draft(peer_id, draft)
    await funnel.record(peer_id, "draft_created", origin="repeat", repeat_of=order.id)
    # Сводка со ссылкой и [Изменить]: счёт выставлен на прошлый пункт и
    # получателя без вопроса, и поменять их клиенту должно быть так же просто,
    # как у «как в прошлый раз» (05.10.2026 кнопка была одна — «Оплатить»).
    invoiced = await conversation._auto_invoice(peer_id, source="invoice_repeat", style="returning")
    if invoiced is None:
        return ToolExecution(
            f"Повтор заказа №{order.id}: черновик со всем составом, доставкой и получателем "
            "готов, но счёт сам не выставился. Спроси клиента «Оформляем?» и после «да» "
            "вызови confirm_order."
        )
    return invoiced


def is_enabled() -> bool:
    return settings.repeat_one_tap_enabled
