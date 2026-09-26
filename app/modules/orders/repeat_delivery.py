"""Доставка «как в прошлый раз» для клиента, который уже заказывал.

Постоянному клиенту незачем заново называть город и искать пункт на карте:
посылку он уже получал, и проще всего спросить «отправить туда же?». Одно
«да» вместо трёх сообщений.

Берём последний **успешный** заказ: оплачен, не возвращён, не отменён и не
вернулся непрошенным. Пункт, куда посылка не доехала или откуда её не
забрали, предлагать первым незачем.

Пункт мы не закрепляем сами — только предлагаем. Он мог закрыться, а
клиент — переехать; поэтому модель спрашивает, и лишь после «да» идёт
обычный `set_delivery_method` с прошлым адресом. Инструмент заново найдёт
пункт, проверит, что он принимает посылки, и посчитает цену.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.core.database import get_session_factory
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

# Подпись пункта СДЭКа в деталях заказа: «СДЭК, пункт выдачи: <адрес>».
_CDEK_PVZ_PREFIX = "СДЭК, пункт выдачи: "


@dataclass(frozen=True)
class LastDelivery:
    order_id: int
    method: str
    city: str
    # Адрес пункта (Ozon, СДЭК) или адрес доставки курьером.
    place: str

    def spoken(self) -> str:
        """Как назвать клиенту."""
        if self.method == "ozon_pvz":
            return f"пункт выдачи Ozon: {self.place}"
        if self.method == "cdek_pvz":
            return f"пункт выдачи СДЭК: {self.place}"
        return f"курьер СДЭК по адресу {self.place}"

    def tool_call(self) -> str:
        """Каким вызовом повторить — дословно, чтобы модель не додумывала."""
        if self.method == "cdek_courier":
            return f'set_delivery_method(method="cdek_courier", address="{self.place}")'
        return (
            f'set_delivery_method(method="{self.method}", address="{self.city}", '
            f'pickup_point="{self.place}")'
        )


def _from_order(order: Order) -> LastDelivery | None:
    details = order.details or {}
    city = (details.get("address") or "").strip()
    if order.delivery_method == "ozon_pvz":
        place = (details.get("ozon_point_address") or "").strip()
    elif order.delivery_method == "cdek_pvz":
        label = details.get("delivery_label") or ""
        place = label[len(_CDEK_PVZ_PREFIX):].strip() if label.startswith(_CDEK_PVZ_PREFIX) else ""
    elif order.delivery_method == "cdek_courier":
        place = city
    else:
        return None
    if not city or not place:
        return None
    return LastDelivery(order.id, order.delivery_method, city, place)


async def last_for(peer_id: int) -> LastDelivery | None:
    """Куда клиент получил последний удачный заказ. None — такого нет."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return None

    try:
        async with session_factory() as session:
            orders = (
                await session.execute(
                    select(Order)
                    .where(
                        Order.peer_id == peer_id,
                        Order.payment_status == orders_repository.PAID,
                        Order.status.not_in(("refunded", orders_repository.CANCELED)),
                        Order.not_delivered_at.is_(None),
                    )
                    .order_by(Order.created_at.desc())
                    .limit(5)
                )
            ).scalars().all()
    except Exception:
        # Подсказка — удобство, а не условие заказа: без неё клиент просто
        # выберет доставку как обычно.
        logger.exception("Не узнали прошлую доставку peer_id=%s", peer_id)
        return None

    for order in orders:
        found = _from_order(order)
        if found is not None:
            return found
    return None


def suggestion(last: LastDelivery) -> str:
    """Что сказать модели: предложить прошлую доставку и как её повторить."""
    return (
        f"Клиент уже получал заказ №{last.order_id}: {last.spoken()} "
        f"(город {last.city}). Первым делом спроси, отправить ли туда же, — "
        "назови этот адрес дословно. Согласится — вызови ровно "
        f"{last.tool_call()}: инструмент заново найдёт пункт, проверит его "
        "и посчитает цену. Захочет иначе — предложи доставку как обычно."
    )
