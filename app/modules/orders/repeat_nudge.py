"""«Повторить заказ?» — когда чай из вручённого заказа, скорее всего, кончается.

Срок считается по пачкам: `repeat_nudge_days_per_pack` дней на каждую, но
не дольше `repeat_nudge_max_days`. Две пачки по 100 г пьются вдвое дольше
одной, а через три месяца напоминание уже не про этот заказ.

Если у клиента два и больше вручённых заказа, срок не по пачкам, а по его
собственному ритму: медиана промежутков между заказами, от
`repeat_nudge_personal_min_days` до `repeat_nudge_personal_max_days`. Кто
берёт чай раз в месяц, тому «через 21 день» рано, а кто раз в две недели —
поздно.

Заказ, оценённый «Не моё», повторить не предлагаем: в свой срок вместо
этого придёт второй шанс с другим сортом (`second_touch`).

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
from app.modules.orders import feedback, repository as orders_repository, retention
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

# Личный ритм — не раньше этого после вручения.
_AFTER_DELIVERY = timedelta(days=7)


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


async def personal_interval(session, order: Order) -> timedelta | None:
    """Медиана промежутков между заказами клиента — если вручённых два и больше."""
    rows = (
        await session.execute(
            select(Order.created_at, Order.delivered_at).where(
                Order.peer_id == order.peer_id,
                Order.payment_status == orders_repository.PAID,
                Order.status.not_in(("refunded", orders_repository.CANCELED)),
                Order.created_at <= order.created_at,
            ).order_by(Order.created_at)
        )
    ).all()
    if sum(1 for row in rows if row.delivered_at is not None) < 2:
        return None
    moments = [_aware(row.created_at) for row in rows]
    gaps = sorted(later - earlier for earlier, later in zip(moments, moments[1:]))
    middle = len(gaps) // 2
    median = gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2
    low = timedelta(days=settings.repeat_nudge_personal_min_days)
    high = timedelta(days=settings.repeat_nudge_personal_max_days)
    return min(max(median, low), high)


async def due_at(session, order: Order) -> datetime:
    """Когда предлагать повторить: по личному ритму клиента или по пачкам.

    Личный ритм считается от прошлой покупки, а не от вручения: промежутки
    между заказами — это промежутки между покупками. Но не раньше чем через
    неделю после вручения: посылка могла ехать дольше обычного, и «повторить?»
    на следующий день после получения звучит странно.
    """
    delivered = _aware(order.delivered_at)
    interval = await personal_interval(session, order)
    if interval is None:
        return delivered + delay_for(order)
    return max(_aware(order.created_at) + interval, delivered + _AFTER_DELIVERY)


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
    longest = timedelta(days=max(settings.repeat_nudge_max_days, settings.repeat_nudge_personal_max_days))
    touches = []
    async with get_session_factory()() as session:
        for order in await delivered_orders(session, now, longest + shelf):
            due = await due_at(session, order)
            if not retention.ripe(due, due + shelf, now):
                continue
            if await _bought_since(session, order):
                continue
            if await feedback.rating_of(order.id) == "no":
                # «Не моё» — повторять нечего; вместо этого второй шанс.
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
