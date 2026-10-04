"""Блок «Покупки клиента» для промпта: что брал, как оценил — для подбора.

Раньше модель видела прошлые заказы только ради доставки и получателя
(«как в прошлый раз») и последний заказ для «Повторить». Советовать по
вкусу клиента ей было не на что: «вы брали Те Гуань Инь» она могла сказать,
только если клиент сам об этом напомнил.

Здесь — до пяти последних заказов: дата, состав, статус, оценка, начало
отзыва. **Без ФИО, телефонов, почты и адресов**: для подбора они не нужны,
а всё, что уходит в API модели, — трансграничная передача.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.core import worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import templates
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order, OrderFeedback, OrderRating

logger = logging.getLogger(__name__)

LIMIT = 5
_REVIEW_CHARS = 100
_RATING = {"great": "понравился", "ok": "нормально", "no": "не понравился"}

INSTRUCTION = (
    "Используй историю покупок для подбора: «вы брали Те Гуань Инь — к нему…». "
    "Не пересказывай её без повода и не перечисляй прошлые заказы, если клиент не "
    "спрашивал. Не предлагай сорт, который клиенту не понравился. Оценки "
    "дословно не упоминай."
)


def _status(order: Order) -> str:
    if order.status == "refunded":
        return "возвращён"
    if order.status == orders_repository.CANCELED:
        return "отменён"
    if order.not_delivered_at is not None:
        return "не вручён"
    if order.delivered_at is not None:
        return "вручён"
    return "в пути"


async def context(peer_id: int) -> str:
    """Блок для промпта или пустая строка, если заказов нет."""
    if not settings.purchase_history_in_prompt_enabled:
        return ""
    try:
        async with get_session_factory()() as session:
            orders = (
                await session.execute(
                    select(Order)
                    .where(Order.peer_id == peer_id, Order.payment_status == orders_repository.PAID)
                    .order_by(Order.created_at.desc())
                    .limit(LIMIT)
                )
            ).scalars().all()
            if not orders:
                return ""
            ids = [order.id for order in orders]
            ratings = dict((await session.execute(
                select(OrderRating.order_id, OrderRating.rating).where(OrderRating.order_id.in_(ids))
            )).all())
            reviews = dict((await session.execute(
                select(OrderFeedback.order_id, OrderFeedback.text).where(OrderFeedback.order_id.in_(ids))
            )).all())
    except Exception:
        logger.exception("Не собрали историю покупок для peer_id=%s", peer_id)
        return ""

    lines = ["Покупки клиента (последние сначала):"]
    for order in orders:
        line = (
            f"- {worktime.to_msk(order.created_at).strftime('%d.%m.%Y')} — "
            f"{templates.composition(order.items or [])} — {_status(order)}"
        )
        if order.id in ratings:
            line += f"; оценка: {_RATING.get(ratings[order.id], ratings[order.id])}"
        review = " ".join((reviews.get(order.id) or "").split())
        if review:
            cut = review if len(review) <= _REVIEW_CHARS else review[: _REVIEW_CHARS - 1] + "…"
            line += f"; отзыв: «{cut}»"
        lines.append(line)
    lines.append(INSTRUCTION)
    return "\n".join(lines)
