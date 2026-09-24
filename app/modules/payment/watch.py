"""Догляд за оплатой и чеками.

Нужен по двум причинам, и первая важнее.

**Уведомление может не дойти.** ЮKassa повторяет его сутки, но если всё это
время наш контейнер отвечал ошибкой, оплаченный заказ так и останется
неотправленным: денег ждали, деньги пришли, а отправление никто не завёл.
Поэтому заказы в ожидании оплаты перечитываются у ЮKassa по таймеру — это
страховка на случай, когда вебхук подвёл.

**Чек регистрирует не ЮKassa.** Она лишь передаёт данные, а создают чек
касса с ОФД — позже и асинхронно. Документация прямо говорит: если чек
висит в `pending` трое суток, идти в поддержку. Значит за этим надо
следить, иначе узнаем от налоговой.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import client as client_messages, templates
from app.modules.orders import order_chat, repository as orders_repository
from app.modules.orders.models import Order
from app.modules.payment import service as payment_service, webhook, yookassa_client

logger = logging.getLogger(__name__)

# Сколько ждать оплаты, прежде чем показать заказ менеджеру. Ссылка живёт
# ограниченное время, и висящий сутки заказ — это либо передумавший клиент,
# либо потерянное уведомление; и то и другое человеку стоит увидеть.
_UNPAID_AFTER = timedelta(hours=24)
# Срок из документации ЮKassa: дольше — в поддержку.
_RECEIPT_STUCK_AFTER = timedelta(days=3)
# Совсем старые не трогаем: опрашивать их по кругу незачем.
_GIVE_UP_AFTER = timedelta(days=7)
_BATCH = 20

STATUS_UNPAID = "payment_expired"
RECEIPT_STUCK = "stuck"


async def check_pending() -> dict:
    """Перечитать у ЮKassa всё, что ещё не досчитано."""
    result = {"checked": 0, "paid": 0, "canceled": 0, "unpaid": 0, "receipts": 0, "failed": 0}

    if not payment_service.is_enabled():
        result["skipped"] = "оплата не подключена"
        return result

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        logger.warning("Проверка платежей пропущена: база недоступна")
        result["skipped"] = "база недоступна"
        return result

    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Order)
                .where(
                    Order.payment_id.isnot(None),
                    Order.created_at > now - _GIVE_UP_AFTER,
                    or_(
                        Order.status == payment_service.STATUS_AWAITING_PAYMENT,
                        # Оплачен, но чек ещё не зарегистрирован. Смотрим на
                        # статус платежа, а не заказа: у заказа СДЭКом после
                        # оплаты в `status` лежит состояние доставки, и по
                        # нему чек потерялся бы из виду.
                        (Order.payment_status == orders_repository.PAID)
                        & (Order.receipt_status.notin_(["succeeded", RECEIPT_STUCK])),
                    ),
                )
                .order_by(Order.created_at)
                .limit(_BATCH)
            )
        ).scalars().all()

    for order in rows:
        result["checked"] += 1
        try:
            payment = await yookassa_client.get_payment(order.payment_id)
        except Exception:
            logger.exception("Не узнали состояние платежа по заказу %s", order.id)
            result["failed"] += 1
            continue

        age = now - order.created_at

        if order.status == payment_service.STATUS_AWAITING_PAYMENT:
            if payment.status == "succeeded" and payment.paid:
                # Уведомление не дошло, а деньги есть. Проводим тем же кодом,
                # что и вебхук: он умеет не делать работу дважды.
                logger.warning(
                    "Заказ %s оплачен, но уведомление не дошло — доводим сами", order.id
                )
                await webhook.handle_paid(payment)
                result["paid"] += 1
                continue

            if payment.status == "canceled":
                await orders_repository.set_state(
                    order.id, status=webhook.STATUS_CANCELED, payment_status=payment.status
                )
                result["canceled"] += 1
                await client_messages.send(
                    peer_id=order.peer_id,
                    ref=client_messages.order_ref(order.id),
                    event_type=templates.PAYMENT_DECLINED,
                    text=templates.payment_declined(order),
                )
                continue

            if age > _UNPAID_AFTER:
                await orders_repository.set_state(
                    order.id, status=STATUS_UNPAID, payment_status=payment.status
                )
                result["unpaid"] += 1
                await order_chat.send(
                    order,
                    templates.manager_unpaid(
                        order, payment.status, settings.payment_unpaid_after_hours
                    ),
                )
                # Ссылка к этому моменту уже не работает, и клиент, который
                # собирался оплатить завтра, упёрся бы в неё молча. Лучше
                # сказать прямо и позвать оформить заново.
                await client_messages.send(
                    peer_id=order.peer_id,
                    ref=client_messages.order_ref(order.id),
                    event_type=templates.PAYMENT_EXPIRED,
                    text=templates.payment_expired(order),
                )
            continue

        # Оплачен: следим за чеком.
        if payment.receipt_registration and payment.receipt_registration != order.receipt_status:
            await orders_repository.set_state(order.id, receipt_status=payment.receipt_registration)
            result["receipts"] += 1

        if payment.receipt_registration == "succeeded":
            continue

        if payment.receipt_registration == "canceled" or age > _RECEIPT_STUCK_AFTER:
            await orders_repository.set_state(order.id, receipt_status=RECEIPT_STUCK)
            await order_chat.send(
                order,
                templates.manager_receipt_stuck(
                    order,
                    payment.receipt_registration,
                    overdue=payment.receipt_registration != "canceled",
                ),
            )
            # Клиент ждёт чек и не знает, что тот застрял на стороне кассы.
            # Ждать от него вопроса «а где чек» — значит отвечать на него
            # задним числом.
            details = order.details or {}
            await client_messages.send(
                peer_id=order.peer_id,
                ref=client_messages.order_ref(order.id),
                event_type=templates.RECEIPT_DELAYED,
                text=templates.receipt_delayed(
                    order,
                    email=details.get("recipient_email", ""),
                    phone=details.get("recipient_phone", ""),
                ),
            )

    return result
