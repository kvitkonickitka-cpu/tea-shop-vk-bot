"""События доставки: посылку передали перевозчику, вручили, не вручили.

Событие ставится **один раз**, кто бы о нём ни узнал первым — опрос
перевозчика (`delivery_watch`) или ручная команда менеджера
(`scripts/api.sh orders/<N>/delivered`). Отметка в заказе ставится
условным `UPDATE … WHERE <отметка> IS NULL`: кто её поставил, тот и
выполняет следствия. Это важно для «вручено»: по нему уходит закрывающий
чек, и дважды он уйти не должен.

«Вручено» и «не вручено» — конечные и взаимоисключающие: посылку либо
получили, либо она едет обратно. Вручение подразумевает и передачу —
если опрос проспал промежуточный статус, отметка передачи ставится вместе
с вручением, но отдельного сообщения клиенту о ней уже нет.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from app.core import worktime
from app.core.database import get_session_factory
from app.messages import client as client_messages, templates
from app.modules.orders import order_chat, repository as orders_repository
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

HANDED_OVER = "order.handed_over"
DELIVERED = "order.delivered"
NOT_DELIVERED = "order.not_delivered"

EVENTS = (HANDED_OVER, DELIVERED, NOT_DELIVERED)

# Откуда узнали. Пишется в лог и в ответ ручной команды.
SOURCE_CARRIER = "перевозчик"
SOURCE_MANUAL = "вручную"

# Сколько после события ещё досылать клиенту новость, если она пришлась на
# тихие часы. Больше трёх суток — новость уже не новость.
_TELL_WITHIN = timedelta(days=3)

# Страница отслеживания СДЭКа — по номеру накладной.
CDEK_TRACKING_URL = "https://www.cdek.ru/ru/tracking"


def _carrier_name(order: Order) -> str:
    if order.ozon_posting:
        return "Ozon"
    if order.cdek_uuid:
        return "СДЭКом"
    return "службой доставки"


async def record(order_id: int, event: str, *, source: str) -> Order | None:
    """Отметить событие доставки. None — оно уже было, или заказа нет.

    Следствия (сообщение клиенту, менеджеру, закрывающий чек) выполняет
    тот, кто поставил отметку, — ровно один раз.
    """
    if event not in EVENTS:
        raise ValueError(f"неизвестное событие доставки: {event}")

    now = datetime.now(timezone.utc)
    statement = update(Order).where(Order.id == order_id)
    if event == HANDED_OVER:
        statement = statement.where(Order.handed_over_at.is_(None)).values(handed_over_at=now)
    else:
        # Конечные события взаимоисключающие: не вручённую посылку нельзя
        # потом «вручить» опросом, и наоборот. Ошибку ручной отметки
        # поправит человек в базе — автоматике здесь лучше не решать.
        statement = statement.where(
            Order.delivered_at.is_(None), Order.not_delivered_at.is_(None)
        )
        if event == DELIVERED:
            statement = statement.values(delivered_at=now)
        else:
            statement = statement.values(not_delivered_at=now)

    session_factory = get_session_factory()
    async with session_factory() as session:
        order = (
            await session.execute(statement.returning(Order))
        ).scalar_one_or_none()
        if order is not None and event != HANDED_OVER and order.handed_over_at is None:
            # Вручить, не передав, нельзя: опрос мог проспать промежуточный
            # статус. Отметку ставим, отдельной новости о ней не будет.
            order.handed_over_at = now
        await session.commit()

    if order is None:
        return None

    logger.info("Заказ %s: событие %s (%s)", order_id, event, source)
    await _consequences(order, event)
    return order


async def _consequences(order: Order, event: str) -> None:
    """Что происходит после события. Сбой одного следствия не отменяет другие."""
    if event == DELIVERED:
        from app.modules.payment import settlement

        try:
            await settlement.on_delivered(order)
        except Exception:
            # Отметка «вручено» уже стоит, и повторно событие не придёт. Чек
            # досылает таймер (`settlement.check`) или команда
            # orders/<N>/settlement-receipt.
            logger.exception("Заказ %s: закрывающий чек не отправлен", order.id)

    if event == NOT_DELIVERED:
        await order_chat.send(
            order, templates.manager_not_delivered(order, order.carrier_status or "не вручён")
        )

    await tell_client(order)


def _latest_event(order: Order) -> str | None:
    if order.not_delivered_at is not None:
        return NOT_DELIVERED
    if order.delivered_at is not None:
        return DELIVERED
    if order.handed_over_at is not None:
        return HANDED_OVER
    return None


async def tell_client(order: Order, *, now: datetime | None = None) -> bool:
    """Сказать клиенту о последнем событии доставки. True — написали.

    Только оплаченным заказам: неоплаченный ведёт менеджер, и лезть к
    клиенту с новостями о посылке вперёд него незачем. Ночью молчим — тик
    расписания дошлёт утром. Одна новость на заказ и событие: отметка в
    журнале отправок срежет повтор.
    """
    if order.payment_status != orders_repository.PAID:
        return False
    if worktime.is_quiet(now):
        return False

    event = _latest_event(order)
    if event is None:
        return False

    details = order.details or {}
    if event == HANDED_OVER:
        if order.ozon_posting:
            text = templates.handed_over(
                order, carrier=_carrier_name(order), number=order.ozon_posting
            )
        else:
            text = templates.handed_over(
                order,
                carrier=_carrier_name(order),
                number=details.get("cdek_number", ""),
                tracking_url=CDEK_TRACKING_URL if details.get("cdek_number") else "",
            )
        event_type = templates.HANDED_OVER
    elif event == DELIVERED:
        from app.modules.payment import settlement

        # Про закрывающий чек предупреждаем, только если он и правда ушёл:
        # иначе клиент ждал бы письма, которого не будет.
        text = templates.delivered(
            order,
            receipt_email=details.get("recipient_email", "") if settlement.was_sent(order) else "",
        )
        event_type = templates.DELIVERED
    else:
        text = templates.not_delivered(order)
        event_type = templates.NOT_DELIVERED

    return await client_messages.send(
        peer_id=order.peer_id,
        ref=client_messages.order_ref(order.id),
        event_type=event_type,
        text=text,
    )


async def tell_pending_clients(now: datetime | None = None) -> int:
    """Дослать новости, которые пришлись на тихие часы.

    Проходит по недавним событиям каждый тик: повтор срезает журнал
    отправок, так что лишнего клиент не получит.
    """
    now = now or datetime.now(timezone.utc)
    if worktime.is_quiet(now):
        return 0

    since = now - _TELL_WITHIN
    session_factory = get_session_factory()
    async with session_factory() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.payment_status == orders_repository.PAID,
                    or_(
                        Order.handed_over_at > since,
                        Order.delivered_at > since,
                        Order.not_delivered_at > since,
                    ),
                )
            )
        ).scalars().all()

    told = 0
    for order in orders:
        event_type = {
            HANDED_OVER: templates.HANDED_OVER,
            DELIVERED: templates.DELIVERED,
            NOT_DELIVERED: templates.NOT_DELIVERED,
        }.get(_latest_event(order))
        if event_type is None:
            continue
        if await client_messages.already_sent(client_messages.order_ref(order.id), event_type):
            continue
        if await tell_client(order, now=now):
            told += 1
    return told
