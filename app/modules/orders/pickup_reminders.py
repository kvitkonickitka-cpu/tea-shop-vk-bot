"""Посылка в пункте выдачи: напоминание на 2-й день и за день до конца хранения (C4).

Флаг `pickup_reminders`. Срок хранения — от перевозчика (`orders.storage_until`),
а если он его не отдал — с прибытия плюс `PICKUP_STORAGE_DAYS_OZON` /
`PICKUP_STORAGE_DAYS_CDEK` (вводная, сверить с договорами). В тексте — дата,
адрес пункта из заказа и где взять код получения.

Сообщения служебные: «стоп» на продающие их не гасит, но ночью (тихие часы)
не уходят — дошлёт утренний тик. По разу на заказ: первое — `pickup_reminder_1`,
второе — тот же ключ журнала, что прежнее «срок хранения заканчивается»
(`pickup_expiring`), чтобы клиент не получил два одинаковых. Невыкуп
(`not_delivered`) считается в `analytics.v_delivery_outcomes`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.core import flags, worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import client as client_messages, funnel, templates
from app.modules.orders import delivery_events
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)


def storage_end(order: Order) -> date:
    if order.storage_until:
        return order.storage_until
    days = settings.pickup_storage_days_ozon if order.ozon_posting else settings.pickup_storage_days_cdek
    arrived = worktime.to_msk(order.at_pickup_at).date()
    return arrived + timedelta(days=days)


async def check(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if worktime.is_quiet(now):
        return {"skipped": "тихие часы"}
    today = worktime.to_msk(now).date()
    async with get_session_factory()() as session:
        orders = (await session.execute(select(Order).where(
            Order.payment_status == "succeeded", Order.at_pickup_at.is_not(None),
            Order.delivered_at.is_(None), Order.not_delivered_at.is_(None),
            Order.status.notin_(("refunded", "canceled")),
            Order.at_pickup_at > now - timedelta(days=30),
        ))).scalars().all()
    sent = {"pickup_reminder_1": 0, "pickup_reminder_2": 0}
    for order in orders:
        if not await flags.enabled_for(flags.PICKUP_REMINDERS, order.peer_id):
            continue
        end = storage_end(order)
        arrived = worktime.to_msk(order.at_pickup_at).date()
        carrier = "Ozon" if order.ozon_posting else "СДЭК"
        address = delivery_events._point_address(order)
        if today >= end - timedelta(days=1) and today <= end:
            # За день до конца (или в последний день, если накануне не успели).
            if await client_messages.send(
                peer_id=order.peer_id, ref=client_messages.order_ref(order.id), event_type=templates.PICKUP_EXPIRING,
                text=templates.pickup_reminder(order, carrier=carrier, address=address, storage_until=end, last_day=True),
            ):
                await funnel.record(order.peer_id, "pickup_reminder_2", order_id=order.id, source_=funnel.REMINDER)
                sent["pickup_reminder_2"] += 1
            continue
        if today >= arrived + timedelta(days=1) and today < end - timedelta(days=1):
            if await client_messages.send(
                peer_id=order.peer_id, ref=client_messages.order_ref(order.id), event_type=templates.PICKUP_REMINDER_1,
                text=templates.pickup_reminder(order, carrier=carrier, address=address, storage_until=end, last_day=False),
            ):
                await funnel.record(order.peer_id, "pickup_reminder_1", order_id=order.id, source_=funnel.REMINDER)
                sent["pickup_reminder_1"] += 1
    return sent
