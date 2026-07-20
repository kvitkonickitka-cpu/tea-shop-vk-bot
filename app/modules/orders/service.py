import logging
from typing import Any

from app.core.config import settings
from app.modules.delivery import cdek_client
from app.modules.dialog import claude_client, vk_client
from app.modules.orders import vk_orders_client

logger = logging.getLogger(__name__)


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

    # Логируем сырой объект заказа: пока не проверяли вживую точную схему
    # полей VK для этого события, это нужно для быстрой диагностики.
    logger.info("Fetched order %s: %s", order_id, order)

    user_id = order.get("user_id")
    address = order.get("delivery_address") or order.get("address")

    if not user_id:
        logger.error("Order %s has no user_id, cannot notify buyer", order_id)
        return

    total_price = order.get("total_price")
    if isinstance(total_price, dict):
        items_total = total_price.get("amount", 0) / 100
    else:
        items_total = order.get("price", 0)

    if not address:
        logger.warning("Order %s has no delivery address, skipping CDEK calc", order_id)
        reply = "Ваш заказ принят! Уточним стоимость доставки и напишем отдельно."
        await vk_client.send_message(user_id, reply)
        return

    items = order.get("items", [])
    total_quantity = sum(item.get("quantity", 1) for item in items) or 1
    weight_grams = settings.cdek_default_package_weight_grams * total_quantity

    try:
        tariff = await cdek_client.calculate_cheapest_tariff(address, weight_grams)
        delivery_cost = tariff["delivery_sum"]
    except Exception:
        logger.exception("Failed to calculate CDEK delivery for order %s", order_id)
        reply = "Ваш заказ принят! Точную стоимость доставки СДЭК уточним и напишем вам отдельно."
        await vk_client.send_message(user_id, reply)
        return

    facts = (
        f"Заказ №{order_id} принят. Сумма товаров: {items_total} руб. "
        f"Доставка СДЭК до адреса «{address}»: {delivery_cost} руб. "
        f"Итого с доставкой: {items_total + delivery_cost} руб."
    )

    try:
        reply = await claude_client.generate_order_notification(facts)
    except Exception:
        logger.exception("Claude order notification generation failed for order %s", order_id)
        reply = facts

    await vk_client.send_message(user_id, reply)
