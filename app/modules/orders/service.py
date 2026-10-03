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

from app.core.config import settings
from app.messages import client as client_messages, manager as manager_messages, templates
from app.modules.dialog import vk_client
from app.modules.orders import address as address_parser, contacts, order_chat, state, vk_orders_client
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


def _address_of(order: dict) -> str:
    """Адрес доставки из заказа ВК — в объекте `delivery`, у старых схем — в корне."""
    delivery = order.get("delivery") if isinstance(order.get("delivery"), dict) else {}
    address = delivery.get("address") or order.get("delivery_address") or order.get("address")
    if isinstance(address, dict):
        address = address.get("address") or address.get("text")
    return str(address or "").strip()


def _recipient_of(order: dict) -> tuple[str, str] | None:
    """Получатель из заказа ВК (`recipient.name`, `recipient.phone`) — если телефон настоящий."""
    raw = order.get("recipient") if isinstance(order.get("recipient"), dict) else {}
    name = str(raw.get("name") or "").strip()
    phone = contacts.normalize_phone(str(raw.get("phone") or ""))
    return (name, phone) if name and phone else None


async def _direct_points(
    user_id: int, order_id, order: dict, items: list[dict], items_total: float, address: str
) -> bool:
    """Сразу пункты Ozon рядом с адресом из заказа. False — прежний вопрос «в какой город».

    Тот же поиск, что и «город и улица» в диалоге: до четырёх пунктов с
    номерами, первыми — на этой улице, с ценой у каждого, если Ozon ответил
    вовремя; список ложится в черновик, и выбор «1», кнопкой или адресом идёт
    обычным путём до автоматического счёта.
    """
    from app.messages import keyboard as keyboards
    from app.modules.orders import buttons, conversation, repeat_delivery

    if not settings.storefront_direct_points_enabled:
        return False
    parsed = address_parser.city_and_street(address)
    if parsed is None:
        return False
    city, street = parsed
    try:
        await conversation._execute_set_delivery_method(
            user_id, {"method": "ozon_pvz", "address": city, "pickup_point": street}
        )
    except Exception:
        logger.exception("Витринный заказ %s: пункты рядом с адресом не подобрали", order_id)
        return False
    draft = await state.get_draft(user_id)
    shown = list((draft.details.get("shown_points") if draft else None) or [])
    if draft is None or not shown or draft.details.get("ozon_point_id"):
        return False

    last = await repeat_delivery.last_recipient_for(user_id)
    candidate = _recipient_of(order)
    if last is not None and last.email:
        ask = templates.storefront_ask_last(last.name, last.phone, last.email)
    elif candidate is not None:
        draft.details["storefront_recipient"] = {"name": candidate[0], "phone": candidate[1]}
        await state.set_draft(user_id, draft)
        ask = templates.storefront_ask_email(*candidate)
    else:
        ask = templates.STOREFRONT_ASK_ALL

    keyboard, hint = await buttons.for_reply(user_id)
    shows_buttons = await keyboards.for_peer(user_id, keyboard) is not None
    text = templates.storefront_points(
        order_id=order_id, items=items, items_total=items_total, shown=shown,
        per_point_prices=all(point.get("price") is not None for point in shown),
        delivery_cost=draft.delivery_cost, ask=ask, hint=hint if shows_buttons else "",
    )
    await client_messages.send(
        peer_id=user_id, ref=f"vk_order:{order_id}", event_type=templates.STOREFRONT_ORDER,
        text=text, keyboard=keyboard,
    )
    return True


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

    # Сырой объект в лог не пишем: в нём имя, телефон и адрес покупателя.
    # Для разбора схемы хватает ключей — по ним видно, где лежат адрес и
    # получатель.
    logger.info(
        "Заказ витрины %s: поля %s, delivery %s, recipient %s",
        order_id, sorted(order.keys()),
        sorted((order.get("delivery") or {}).keys()) if isinstance(order.get("delivery"), dict) else "—",
        sorted((order.get("recipient") or {}).keys()) if isinstance(order.get("recipient"), dict) else "—",
    )

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
            f"Заказ №{order_id} принят! Проверим состав и напишем здесь расчёт "
            "доставки 🙏",
        )
        return

    items_total = round(sum(item["price"] * item["quantity"] for item in items), 2)

    # Черновик ставим на тот же этап, на который его ставит propose_order в
    # переписке: дальше клиента ведут обычные инструменты.
    draft = OrderDraft(items=items, items_total=items_total, stage="awaiting_delivery")

    # Адрес из витрины — подсказка, а не выбор: доставку всё равно считает
    # перевозчик, а пункт выдачи клиент выбирает сам из показанных.
    address = _address_of(order)
    if address:
        draft.details["vk_order_address"] = address
    draft.details["vk_order_id"] = order_id

    await state.set_draft(user_id, draft)

    if address and await _direct_points(user_id, order_id, order, items, items_total, address):
        listed = ", ".join(f"{item['name']} × {item['quantity']}" for item in items)
        await _tell_manager(
            order_id, user_id,
            f"{listed} — {templates.amount(items_total)} ₽. Клиенту показаны пункты рядом с адресом из заказа.",
        )
        return
    # Прежний путь: пункты не подобрали — черновик возвращаем к выбору доставки.
    await state.set_draft(user_id, OrderDraft(
        items=items, items_total=items_total, stage="awaiting_delivery",
        details={key: value for key, value in (("vk_order_address", address), ("vk_order_id", order_id)) if value},
    ))

    listed = ", ".join(f"{item['name']} × {item['quantity']}" for item in items)
    lines = [
        f"Заказ №{order_id} принят: {listed} — {templates.amount(items_total)} ₽.",
        "Осталось выбрать доставку. Дешевле всего пункт выдачи Ozon — "
        "заберёте сами. Быстрее, но дороже — пункт выдачи СДЭК, ещё есть "
        "курьер СДЭК до двери.",
    ]
    lines.append(
        f"В какой город везём? Адрес из заказа — «{address}», везём туда?"
        if address
        else "В какой город везём?"
    )
    # Через журнал отправок: одно событие — одно сообщение, и сказанное
    # ботом попадает в историю диалога.
    await client_messages.send(
        peer_id=user_id, ref=f"vk_order:{order_id}", event_type=templates.STOREFRONT_ORDER,
        text="\n".join(lines),
    )

    await _tell_manager(
        order_id,
        user_id,
        f"{listed} — {templates.amount(items_total)} ₽. Клиенту предложено выбрать доставку в диалоге.",
    )
