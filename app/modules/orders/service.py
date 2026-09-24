"""Заказ, оформленный в витрине сообщества, минуя диалог.

ВК присылает `market_order_new`, когда клиент нажал «Оформить» в товарах
сообщества. Дальше заказ ведёт тот же путь, что и заказ из переписки:
выбор доставки инструментами, живой расчёт у перевозчика, счёт ЮKassa,
отправление после оплаты.

Раньше эта ветка жила отдельной жизнью: считала СДЭК сама, называла цену
тарифа без НДС и сборов (ту самую, из-за которой 320 руб расчёта
превращались в 397 руб счёта), не знала про Ozon и не умела выставить
счёт. Клиент из витрины и клиент из переписки получали разные магазины.
"""

import logging
from typing import Any

from app.messages import manager as manager_messages
from app.modules.dialog import vk_client
from app.modules.orders import order_chat, state, vk_orders_client
from app.modules.orders.state import OrderDraft

logger = logging.getLogger(__name__)


def _rubles(value: Any) -> float:
    """Сумма ВК в рублях: в API она приходит копейками и строкой."""
    if isinstance(value, dict):
        value = value.get("amount", 0)
    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


def _items_of(raw_items: list[dict]) -> list[dict]:
    """Состав заказа в том виде, в каком его держит черновик.

    Схему ВК разбираем защитно: товар без названия или цены пропускаем, но
    из-за него не теряем весь заказ.
    """
    items = []
    for row in raw_items or []:
        product = row.get("item") or {}
        name = (product.get("title") or "").strip()
        if not name:
            continue
        quantity = int(row.get("quantity") or 1)
        price = _rubles(product.get("price"))
        if not price:
            # Цена за единицу неизвестна — берём из строки заказа целиком.
            price = round(_rubles(row.get("price")) / max(quantity, 1), 2)
        items.append({"name": name, "quantity": quantity, "price": price})
    return items


async def _tell_manager(order_id: int, user_id: int, text: str) -> None:
    await manager_messages.notify(
        manager_messages.STOREFRONT_ORDER,
        f"🛒 <b>Заказ из витрины №{order_id}</b>\n{text}\n\n"
        f"{vk_client.dialog_link(user_id)}",
        peer_id=user_id,
        chat_id=order_chat.chat_id(),
    )


async def handle_new_order(order_event: dict[str, Any]) -> None:
    order_id = order_event.get("id") or order_event.get("order_id")
    if order_id is None:
        logger.error("market_order_new event without order id: %s", order_event)
        return

    try:
        order = await vk_orders_client.get_order(order_id)
    except Exception:
        logger.exception("Failed to fetch order %s from VK", order_id)
        return

    # Логируем сырой объект заказа: точную схему полей ВК для этого события
    # вживую пока не проверяли, и при первом настоящем заказе это первое,
    # на что придётся смотреть.
    logger.info("Fetched order %s: %s", order_id, order)

    user_id = order.get("user_id")
    if not user_id:
        logger.error("Order %s has no user_id, cannot notify buyer", order_id)
        return

    try:
        raw_items = await vk_orders_client.get_order_items(order_id)
    except Exception:
        logger.exception("Не получили состав витринного заказа %s", order_id)
        raw_items = []

    items = _items_of(raw_items)
    if not items:
        # Без состава черновик не собрать: суммы, объявленная ценность и
        # позиции чека берутся из него. Заказ при этом настоящий, поэтому
        # доводит его человек, а клиент слышит об этом сразу.
        logger.error("Витринный заказ %s: состав не разобрали, зовём менеджера", order_id)
        await _tell_manager(
            order_id, user_id, "Состав заказа не разобрали — оформить доставку руками."
        )
        await vk_client.send_message(
            user_id,
            f"Заказ №{order_id} принят! Сейчас уточним доставку и вернёмся с "
            "расчётом 🙏",
        )
        return

    items_total = round(sum(item["price"] * item["quantity"] for item in items), 2)

    # Черновик ставим на тот же этап, на который его ставит propose_order в
    # переписке: дальше клиента ведут обычные инструменты.
    draft = OrderDraft(items=items, items_total=items_total, stage="awaiting_delivery")

    # Адрес из витрины — подсказка, а не выбор: доставку всё равно считает
    # перевозчик, а пункт выдачи клиент называет сам.
    address = order.get("delivery_address") or order.get("address")
    if isinstance(address, dict):
        address = address.get("address") or address.get("text")
    if address:
        draft.details["vk_order_address"] = str(address)
    draft.details["vk_order_id"] = order_id

    await state.set_draft(user_id, draft)

    listed = ", ".join(f"{item['name']} × {item['quantity']}" for item in items)
    lines = [
        f"Заказ №{order_id} принят: {listed} — {items_total:g} ₽.",
        "Осталось выбрать доставку. Дешевле всего пункт выдачи Ozon — "
        "заберёте сами. Быстрее, но дороже — пункт выдачи СДЭК, ещё есть "
        "курьер СДЭК до двери.",
    ]
    lines.append(
        f"В какой город везём? Адрес из заказа — «{address}», везём туда?"
        if address
        else "В какой город везём?"
    )
    await vk_client.send_message(user_id, "\n".join(lines))

    await _tell_manager(
        order_id,
        user_id,
        f"{listed} — {items_total:g} руб. Клиенту предложено выбрать доставку в диалоге.",
    )
