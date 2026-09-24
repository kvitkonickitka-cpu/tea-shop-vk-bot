"""Заведение отправления у перевозчика.

Отдельным модулем, потому что вызывающих стало два и данные у них разные.
Без оплаты отправление заводит подтверждение заказа — у него на руках
черновик. С оплатой оно заводится после платежа, из уведомления ЮKassa, а
черновика к тому моменту уже нет: данные берутся из сохранённого заказа.

Поэтому функции здесь принимают не черновик и не заказ, а голые значения —
товары, детали, способ доставки. Так один и тот же код работает в обоих
случаях, и не приходится держать две расходящиеся копии.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.core.config import settings
from app.modules.delivery import cdek_client, ozon_client
from app.modules.dialog import telegram_client, vk_client

logger = logging.getLogger(__name__)

CDEK_METHODS = ("cdek_pvz", "cdek_courier")
OZON_METHODS = ("ozon_pvz",)


@dataclass(frozen=True)
class Registered:
    """Что получилось завести. Пустые поля — значит не вышло."""

    cdek_uuid: str | None = None
    ozon_posting: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.cdek_uuid or self.ozon_posting)


def weight_grams(items: list[dict]) -> int:
    """Вес посылки. Пока заглушка: настоящих весов в каталоге нет."""
    quantity = sum(item.get("quantity", 1) for item in items) or 1
    return settings.cdek_default_package_weight_grams * quantity


async def _warn_manager(peer_id: int, carrier: str) -> None:
    text = (
        f"⚠️ Заказ оплачен, но в {carrier} не уехал — завести руками.\n"
        f"Диалог: {vk_client.dialog_link(peer_id)}"
    )
    try:
        # Всё, что про заказы, идёт в свой чат; пусто — значит менеджеру.
        await telegram_client.send_message(
            text, chat_id=settings.telegram_orders_chat_id or None
        )
    except Exception:
        logger.exception("Не смогли предупредить менеджера про заказ peer_id=%s", peer_id)


async def register(
    *,
    peer_id: int,
    delivery_method: str | None,
    items: list[dict],
    details: dict,
    items_total: float,
    delivery_cost: float | None = None,
    order_key: str = "",
) -> Registered:
    """Завести отправление у того перевозчика, которого выбрал клиент."""
    details = details or {}
    number = order_key or f"vk{peer_id}-{int(time.time())}"

    if delivery_method in CDEK_METHODS:
        return Registered(cdek_uuid=await _in_cdek(peer_id, number, items, details))
    if delivery_method in OZON_METHODS:
        return Registered(
            ozon_posting=await _in_ozon(
                peer_id, number, items, details, items_total, delivery_cost
            )
        )
    # Способ без перевозчика — заводить нечего, и это не ошибка.
    return Registered()


async def _in_cdek(peer_id: int, number: str, items: list[dict], details: dict) -> str | None:
    try:
        registered = await cdek_client.register_order(
            number=number,
            tariff_code=details["tariff_code"],
            recipient_name=details["recipient_name"],
            recipient_phone=details["recipient_phone"],
            items=items,
            weight_grams=weight_grams(items),
            to_address=details.get("address"),
            delivery_point=details.get("delivery_point"),
            comment=f"Заказ из ВК, диалог {vk_client.dialog_link(peer_id)}",
        )
    except Exception:
        logger.exception("Не завели заказ в СДЭКе для peer_id=%s", peer_id)
        await _warn_manager(peer_id, "СДЭК")
        return None

    logger.info("Заказ %s заведён в СДЭКе: uuid=%s", number, registered.uuid)
    return registered.uuid


async def _in_ozon(
    peer_id: int,
    number: str,
    items: list[dict],
    details: dict,
    items_total: float,
    delivery_cost: float | None,
) -> str | None:
    try:
        posting = await ozon_client.create_order(
            external_id=number,
            shipment_method_id=settings.ozon_shipment_method_id,
            delivery_point_id=int(details["ozon_point_id"]),
            recipient_name=details["recipient_name"],
            phone_number=details["recipient_phone"],
            items=items,
            weight_grams=weight_grams(items),
            length_mm=settings.ozon_default_length_mm,
            width_mm=settings.ozon_default_width_mm,
            height_mm=settings.ozon_default_height_mm,
            declared_value=items_total,
        )
    except Exception:
        logger.exception("Не завели заказ в Ozon для peer_id=%s", peer_id)
        await _warn_manager(peer_id, "Ozon")
        return None

    # Цену сверяем с тем, что назвали клиенту: Ozon считает заново на
    # создании, и разойтись она может — например, если пункт выбрали другой.
    if delivery_cost is not None and abs(posting.total - delivery_cost) > 1:
        logger.warning(
            "Ozon посчитал доставку иначе, чем мы назвали клиенту: %s против %s (отправление %s)",
            posting.total, delivery_cost, posting.posting_number,
        )

    logger.info(
        "Заказ %s заведён в Ozon: отправление %s, доставка %s руб",
        number, posting.posting_number, posting.total,
    )
    return posting.posting_number
