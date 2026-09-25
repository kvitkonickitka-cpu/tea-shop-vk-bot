"""Опрос перевозчиков: что стало с посылкой после регистрации.

`cdek_watch` отвечает на вопрос «состоялся ли заказ у СДЭКа»; этот модуль —
на следующий: приняли ли посылку в отделении, вручили ли её, не повезли ли
обратно. От вручения зависит закрывающий чек, поэтому узнавать о нём надо
без участия человека.

Вебхуков перевозчиков не заводим: у Ozon их для Доставки нет вовсе, а у
СДЭКа подписка — ещё один публичный адрес со своей проверкой подлинности.
Посылка едет днями, так что часового опроса хватает с запасом: каждый заказ
спрашиваем не чаще `delivery_check_interval_minutes`, за тик — не больше
`_BATCH` заказов, чтобы не упереться ни в лимиты перевозчиков, ни в 60
секунд тика.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, not_, or_, select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import templates
from app.modules.delivery import cdek_client, ozon_client
from app.modules.orders import cdek_watch, delivery_events, order_chat
from app.modules.orders.models import Order
from app.modules.payment import service as payment_service

logger = logging.getLogger(__name__)

# Заказы старше этого срока не опрашиваем: посылка, о которой два месяца
# ничего не известно, — дело менеджера, а не таймера.
_GIVE_UP_AFTER = timedelta(days=60)
_BATCH = 15

# СДЭК: коды статусов из «Клиентского протокола интеграции» v2. Всё, что не
# «создан», «принят в обработку», «удалён» или «некорректный», означает, что
# посылка уже у СДЭКа: мы сдаём её в отделение сами, и первый такой статус —
# «Принят на склад отправителя».
_CDEK_NOT_YET = {"CREATED", "ACCEPTED", "REMOVED", "INVALID"}
_CDEK_DELIVERED = {"DELIVERED", "POSTOMAT_RECEIVED"}
_CDEK_NOT_DELIVERED = {"NOT_DELIVERED"}
_CDEK_TROUBLE = {"REMOVED", "INVALID"}

# Ozon Доставка: статусы отправления из спецификации Delivery API.
_OZON_WITH_CARRIER = {
    "acceptance_in_progress", "on_way", "in_delivery_point", "in_courier_service",
    "delivered",
}
_OZON_TROUBLE = {"forming_failed", "not_accepted_to_delivery"}

# Статусы заказа, которые опрашивать незачем: регистрация у СДЭКа ещё не
# подтверждена (этим занят `cdek_watch`), провалилась, или деньги вернули.
_SKIP_STATUSES = (
    cdek_watch.STATUS_NEW,
    cdek_watch.STATUS_REJECTED,
    cdek_watch.STATUS_STUCK,
    "refunded",
    payment_service.STATUS_UNPAID,
)


@dataclass(frozen=True)
class Observation:
    """Что сказал перевозчик, переведённое на наш язык."""

    status: str
    with_carrier: bool = False
    delivered: bool = False
    not_delivered: bool = False
    trouble: bool = False
    cdek_number: str = ""


def read_cdek(data: dict) -> Observation:
    entity = data.get("entity") or {}
    statuses = entity.get("statuses") or []
    codes = {str(s.get("code") or "") for s in statuses}
    latest = max(statuses, key=lambda s: str(s.get("date_time") or ""), default={})
    latest_code = str(latest.get("code") or "")
    return Observation(
        status=f"СДЭК: {latest.get('name') or latest_code or 'нет статуса'}"
        + (f" ({latest_code})" if latest_code else ""),
        with_carrier=bool(codes - _CDEK_NOT_YET - {""}),
        delivered=bool(codes & _CDEK_DELIVERED),
        not_delivered=bool(codes & _CDEK_NOT_DELIVERED),
        trouble=latest_code in _CDEK_TROUBLE,
        cdek_number=str(entity.get("cdek_number") or ""),
    )


def read_ozon(info: dict, *, handed_over: bool) -> Observation:
    status = str(info.get("status") or "")
    canceled = status == "canceled"
    return Observation(
        status=f"Ozon: {status or 'нет статуса'}",
        with_carrier=status in _OZON_WITH_CARRIER,
        delivered=status == "delivered",
        # Отмена после того, как посылку приняли, — это возврат: клиент не
        # забрал её из пункта. До приёмки отмена — заминка для менеджера.
        not_delivered=canceled and handed_over,
        trouble=status in _OZON_TROUBLE or (canceled and not handed_over),
    )


async def _observe(order: Order) -> Observation:
    if order.ozon_posting:
        info = await ozon_client.posting_info(order.ozon_posting)
        return read_ozon(info, handed_over=order.handed_over_at is not None)
    data = await cdek_client.order_state(order.cdek_uuid)
    return read_cdek(data)


async def _due(now: datetime) -> list[Order]:
    interval = timedelta(minutes=settings.delivery_check_interval_minutes)
    session_factory = get_session_factory()
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Order)
                    .where(
                        or_(Order.cdek_uuid.is_not(None), Order.ozon_posting.is_not(None)),
                        Order.delivered_at.is_(None),
                        Order.not_delivered_at.is_(None),
                        # Заказ Ozon со статусом «confirmed» — обычный: сверку
                        # СДЭКа ждут только заказы с его uuid.
                        not_(
                            and_(
                                Order.status.in_(_SKIP_STATUSES),
                                Order.ozon_posting.is_(None),
                            )
                        ),
                        Order.status != "refunded",
                        Order.created_at > now - _GIVE_UP_AFTER,
                        or_(
                            Order.carrier_checked_at.is_(None),
                            Order.carrier_checked_at < now - interval,
                        ),
                    )
                    .order_by(Order.carrier_checked_at.asc().nulls_first())
                    .limit(_BATCH)
                )
            ).scalars().all()
        )


async def _save(order: Order, observation: Observation | None, now: datetime) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        row = await session.get(Order, order.id)
        if row is None:
            return
        row.carrier_checked_at = now
        if observation is not None:
            row.carrier_status = observation.status
            if observation.cdek_number and (row.details or {}).get("cdek_number") != observation.cdek_number:
                # Номер накладной нужен клиенту в новости о передаче, а та
                # может уйти утром, когда ответа СДЭКа под рукой уже нет.
                row.details = {**(row.details or {}), "cdek_number": observation.cdek_number}
        await session.commit()


async def check_deliveries(now: datetime | None = None) -> dict:
    """Спросить перевозчиков о посылках в пути и отметить события."""
    result = {
        "checked": 0, "handed_over": 0, "delivered": 0, "not_delivered": 0,
        "trouble": 0, "failed": 0, "told": 0,
    }
    try:
        get_session_factory()
    except RuntimeError:
        logger.warning("Опрос доставки пропущен: база недоступна")
        return result

    now = now or datetime.now(timezone.utc)
    for order in await _due(now):
        result["checked"] += 1
        try:
            observation = await _observe(order)
        except Exception as error:
            # Перевозчик не ответил — спросим в следующий раз. Отметку
            # времени ставим всё равно, иначе этот заказ съедал бы весь
            # тик каждые пять минут.
            logger.warning("Заказ %s: статус у перевозчика не узнали — %s", order.id, error)
            result["failed"] += 1
            await _save(order, None, now)
            continue

        changed = observation.status != (order.carrier_status or "")
        await _save(order, observation, now)
        order.carrier_status = observation.status

        if observation.trouble and changed:
            result["trouble"] += 1
            await order_chat.send(order, templates.manager_carrier_trouble(order, observation.status))

        if observation.with_carrier and order.handed_over_at is None:
            if await delivery_events.record(
                order.id, delivery_events.HANDED_OVER, source=delivery_events.SOURCE_CARRIER
            ):
                result["handed_over"] += 1
        if observation.delivered:
            if await delivery_events.record(
                order.id, delivery_events.DELIVERED, source=delivery_events.SOURCE_CARRIER
            ):
                result["delivered"] += 1
        elif observation.not_delivered:
            if await delivery_events.record(
                order.id, delivery_events.NOT_DELIVERED, source=delivery_events.SOURCE_CARRIER
            ):
                result["not_delivered"] += 1

    result["told"] = await delivery_events.tell_pending_clients(now)
    return result
