"""Реактивация: клиент давно не заказывал — что появилось нового или что подойдёт.

Через `reactivation_after_days` после последнего вручения, если с тех пор
нет ни заказа, ни черновика, и реактивацию этому клиенту не присылали
`reactivation_repeat_days`. Срок годности — `reactivation_shelf_days`.

До двух сортов: новинки (столбец «Новинка»), которые клиент не брал; нет
новинок — по правилу второго шанса из «С чем советуем» купленного. Нечего
предложить — не пишем. Без скидок и без давления: одно короткое сообщение.

После реактивации до нового заказа других продающих касаний нет — это
держит общий фильтр (`retention.blocker`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import keyboard as keyboards, templates
from app.messages.models import ClientNotice
from app.modules.orders import feedback, repeat_nudge, retention, second_touch, take
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

_OFFERS = 2


async def _reactivated_recently(session, peer_id: int, now: datetime) -> bool:
    since = now - timedelta(days=settings.reactivation_repeat_days)
    return (await session.execute(
        select(ClientNotice.ref).where(
            ClientNotice.peer_id == peer_id,
            ClientNotice.event_type == templates.REACTIVATION,
            ClientNotice.sent_at > since,
        ).limit(1)
    )).first() is not None


async def candidates(now: datetime) -> list[retention.Touch]:
    after = timedelta(days=settings.reactivation_after_days)
    shelf = timedelta(days=settings.reactivation_shelf_days)
    touches = []
    async with get_session_factory()() as session:
        orders = await repeat_nudge.delivered_orders(session, now, after + shelf)
        last: dict[int, Order] = {}
        for order in orders:
            known = last.get(order.peer_id)
            if known is None or retention.aware(order.delivered_at) > retention.aware(known.delivered_at):
                last[order.peer_id] = order
        for order in last.values():
            due = retention.aware(order.delivered_at) + after
            if not retention.ripe(due, due + shelf, now):
                continue
            if await repeat_nudge._bought_since(session, order):
                continue
            if await retention.already(order.id, templates.REACTIVATION):
                continue
            if await _reactivated_recently(session, order.peer_id, now):
                continue
            touches.append(retention.Touch(
                kind=templates.REACTIVATION, peer_id=order.peer_id, order=order,
                due=due, expires=due + shelf, build=_builder(order),
            ))
    return touches


def _builder(order: Order):
    async def build() -> retention.Message | None:
        from app.modules.catalog import service as catalog_service

        catalog = catalog_service.load_items()
        bought, disliked = await second_touch.purchases(order.peer_id)
        picked = second_touch.pick_sorts(catalog, bought, disliked, limit=_OFFERS, novelties_only=True)
        novelties = bool(picked)
        if not picked:
            picked = second_touch.pick_sorts(catalog, bought, disliked, limit=_OFFERS)
        if not picked:
            logger.info("Клиенту заказа %s для реактивации нечего предложить", order.id)
            return None
        text = templates.reactivation(
            offers=[{
                "name": take.display_name(item["name"]), "price": item["price"],
                "description": second_touch.short_description(item),
            } for item, _ in picked],
            source=picked[0][1], novelties=novelties,
        )
        rows = [[second_touch.take_button(item, templates.REACTIVATION) for item, _ in picked]]
        last_row = []
        if settings.repeat_one_tap_enabled and await feedback.rating_of(order.id) != "no":
            last_row.append(second_touch.repeat_button(order.id, templates.REACTIVATION))
        # «Подобрать чай» — ход модели: нажатие приходит ей текстом, как консультация.
        last_row.append(keyboards.text_button("Подобрать чай", {"a": "advise", "t": templates.REACTIVATION}))
        rows.append(last_row)
        return retention.Message(text, keyboards.inline(rows))
    return build
