"""«Повторить заказ?» — когда чай из вручённого заказа, скорее всего, кончается.

Срок считается по пачкам: `repeat_nudge_days_per_pack` дней на каждую, но
не дольше `repeat_nudge_max_days`. Две пачки по 100 г пьются вдвое дольше
одной, а через три месяца напоминание уже не про этот заказ.

Здесь только то, что касается самого «Повторить»: заказ оплачен, вручён,
не возвращён и не отменён, и с тех пор у клиента нет нового оплаченного
заказа — он и так покупает. Остальные правила (отписка, черновик, вопрос
менеджеру, окно 10–21, пауза между касаниями) общие для всех повторных
касаний и живут в `retention.blocker`.

Один раз на заказ — журналом отправок. Срок годности —
`repeat_nudge_shelf_days`: через неделю после срока «чай подходит к концу»
уже неправда.

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
from app.messages import keyboard as keyboards, templates
from app.modules.orders import repository as orders_repository, retention
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)


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
    touch = templates.REPEAT_NUDGE
    return keyboards.inline([[
        keyboards.text_button("Повторить", {"a": "repeat", "o": order_id, "t": touch}, "positive"),
        keyboards.text_button("Выбрать другое", {"a": "other", "o": order_id, "t": touch}),
    ]])


async def due_at(session, order: Order) -> datetime:
    """Когда предлагать повторить."""
    return _aware(order.delivered_at) + delay_for(order)


async def delivered_orders(session, now: datetime, max_age: timedelta):
    """Оплаченные, вручённые, не возвращённые заказы не старше `max_age`."""
    return (
        await session.execute(
            select(Order).where(
                Order.payment_status == orders_repository.PAID,
                Order.delivered_at.is_not(None),
                Order.delivered_at > now - max_age,
                Order.not_delivered_at.is_(None),
                Order.status.not_in(("refunded", orders_repository.CANCELED)),
            )
        )
    ).scalars().all()


async def candidates(now: datetime) -> list[retention.Touch]:
    shelf = timedelta(days=settings.repeat_nudge_shelf_days)
    touches = []
    async with get_session_factory()() as session:
        for order in await delivered_orders(session, now, timedelta(days=settings.repeat_nudge_max_days) + shelf):
            due = await due_at(session, order)
            if not retention.ripe(due, due + shelf, now):
                continue
            if await _bought_since(session, order):
                continue
            if await retention.already(order.id, templates.REPEAT_NUDGE):
                continue
            touches.append(retention.Touch(
                kind=templates.REPEAT_NUDGE, peer_id=order.peer_id, order=order,
                due=due, expires=due + shelf, build=_builder(order, now),
            ))
    return touches


def _builder(order: Order, now: datetime):
    async def build() -> retention.Message:
        weeks = max(1, round((now - _aware(order.delivered_at)).days / 7))
        return retention.Message(
            templates.repeat_nudge(order.items or [], weeks=weeks), _keyboard(order.id)
        )
    return build


async def check(now: datetime | None = None) -> dict:
    """Только «Повторить заказ?» — по общим правилам повторных касаний."""
    return await retention.check(now, kinds={templates.REPEAT_NUDGE})
