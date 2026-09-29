"""«Повторить заказ?» — когда чай из вручённого заказа, скорее всего, кончается.

Срок считается по пачкам: `repeat_nudge_days_per_pack` дней на каждую, но
не дольше `repeat_nudge_max_days`. Две пачки по 100 г пьются вдвое дольше
одной, а через три месяца напоминание уже не про этот заказ.

Пишем, только если всё сходится:

- заказ оплачен, вручён, не возвращён и не отменён;
- с тех пор у клиента нет нового оплаченного заказа и нет черновика —
  он и так покупает, подталкивать незачем;
- вопрос у менеджера не открыт: разговор ведёт человек;
- клиент не отписывался;
- окно продающих сообщений — 10:00–21:00 по Москве.

Один раз на заказ — журналом отправок. Если срок пришёлся на ночь или на
отписку, которую потом отменили, догоняем не дольше `_LATE_LIMIT`: через
месяц после срока «чай подходит к концу» уже неправда.

Сообщение попадает в историю диалога, так что «да» в ответ модель
понимает как «повторить»: оформляет тот же состав через propose_order, а
доставку «как в прошлый раз» предлагает repeat_delivery.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import client as client_messages, keyboard as keyboards, marketing, templates
from app.modules.dialog import escalation_state
from app.modules.orders import repository as orders_repository, state
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

_LATE_LIMIT = timedelta(days=7)


def delay_for(order: Order) -> timedelta:
    packs = sum(int(item.get("quantity") or 1) for item in (order.items or [])) or 1
    days = min(settings.repeat_nudge_days_per_pack * packs, settings.repeat_nudge_max_days)
    return timedelta(days=days)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def _bought_since(session, order: Order) -> bool:
    newer = (
        await session.execute(
            select(Order.id)
            .where(
                Order.peer_id == order.peer_id,
                Order.id != order.id,
                Order.payment_status == orders_repository.PAID,
                Order.created_at > order.created_at,
            )
            .limit(1)
        )
    ).first()
    return newer is not None


def _keyboard(order_id: int) -> dict | None:
    """«Повторить» — сразу счёт; «Выбрать другое» — разговор с моделью."""
    if not settings.repeat_one_tap_enabled:
        return None
    return keyboards.inline([[
        keyboards.text_button("Повторить", {"a": "repeat", "o": order_id}, "positive"),
        keyboards.text_button("Выбрать другое", {"a": "other", "o": order_id}),
    ]])


async def check(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not marketing.in_window(now):
        return {"sent": 0, "skipped": "вне окна продающих сообщений"}
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return {"sent": 0, "skipped": "нет базы"}

    oldest = now - timedelta(days=settings.repeat_nudge_max_days) - _LATE_LIMIT
    due = []
    async with session_factory() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.payment_status == orders_repository.PAID,
                    Order.delivered_at.is_not(None),
                    Order.delivered_at > oldest,
                    Order.not_delivered_at.is_(None),
                    Order.status.not_in(("refunded", orders_repository.CANCELED)),
                )
            )
        ).scalars().all()
        for order in orders:
            moment = _aware(order.delivered_at) + delay_for(order)
            if not moment <= now <= moment + _LATE_LIMIT:
                continue
            if await _bought_since(session, order):
                continue
            due.append(order)

    sent = 0
    for order in due:
        ref = client_messages.order_ref(order.id)
        if await client_messages.already_sent(ref, templates.REPEAT_NUDGE):
            continue
        if await state.get_draft(order.peer_id) is not None:
            continue
        if await escalation_state.is_open(order.peer_id):
            continue
        if await marketing.is_opted_out(order.peer_id):
            continue
        weeks = max(1, round((now - _aware(order.delivered_at)).days / 7))
        if await client_messages.send(
            peer_id=order.peer_id,
            ref=ref,
            event_type=templates.REPEAT_NUDGE,
            text=templates.repeat_nudge(order.items or [], weeks=weeks),
            keyboard=_keyboard(order.id),
        ):
            sent += 1
            logger.info("Заказ %s: предложили повторить", order.id)
    return {"due": len(due), "sent": sent}
