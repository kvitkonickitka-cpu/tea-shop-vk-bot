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

Здесь же живут напоминания о неоплаченном счёте. **Срок жизни счёта наш, а
не ЮKassa:** она платёж сама не закрывает и никакого «истекает в» в ответе
не присылает — «сутки» всегда были нашим таймером
(`payment_unpaid_after_hours`). От него и считается, когда напомнить и
когда счёт закрыть.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.core import worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import client as client_messages, templates
from app.modules.dialog import history as dialog_history
from app.modules.dialog.models import ConversationMessage
from app.modules.orders import order_chat, repository as orders_repository, state
from app.modules.orders.models import Order
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service, webhook, yookassa_client

logger = logging.getLogger(__name__)

# Срок из документации ЮKassa: дольше — в поддержку.
_RECEIPT_STUCK_AFTER = timedelta(days=3)
# Совсем старые не трогаем: опрашивать их по кругу незачем.
_GIVE_UP_AFTER = timedelta(days=7)
_BATCH = 20

RECEIPT_STUCK = "stuck"


async def _dialogue_is_live(order: Order) -> bool:
    """Идёт ли разговор прямо сейчас — тогда напоминание лишнее.

    Два случая. Клиент написал только что: он в диалоге, и робот с
    напоминанием выглядит глухим. Или менеджер ответил после выставления
    счёта: заказ ведёт человек, и бот не должен лезть поперёд него.
    """
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return False

    talked_after = datetime.now(timezone.utc) - timedelta(
        minutes=settings.payment_reminder_skip_if_talked_minutes
    )
    async with session_factory() as session:
        client_said = await session.scalar(
            select(ConversationMessage.id)
            .where(
                ConversationMessage.peer_id == order.peer_id,
                ConversationMessage.role == "user",
                ConversationMessage.created_at > talked_after,
            )
            .limit(1)
        )
        if client_said is not None:
            return True

        manager_said = await session.scalar(
            select(ConversationMessage.id)
            .where(
                ConversationMessage.peer_id == order.peer_id,
                ConversationMessage.author == dialog_history.AUTHOR_MANAGER,
                ConversationMessage.created_at > order.created_at,
            )
            .limit(1)
        )
    return manager_said is not None


def _created_at(order: Order) -> datetime:
    """Момент создания заказа со часовым поясом: база отдаёт его наивным."""
    created = order.created_at
    return created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created


def expires_at(order: Order) -> datetime:
    """Когда счёт закрывается. Срок наш, поэтому считается от создания."""
    return _created_at(order) + timedelta(hours=settings.payment_unpaid_after_hours)


async def _remind(order: Order, payment: yookassa_client.Payment, now: datetime) -> str | None:
    """Напомнить про неоплаченный счёт, если пора и если это уместно.

    Возвращает тип отправленного напоминания или None. Тихие часы не
    отменяют напоминание, а откладывают его: тик расписания придёт снова
    утром, и отметки в базе всё ещё пусты. Исключение — второе напоминание:
    если к утру счёт уже закроется, оно бессмысленно, и мы его пропускаем.
    """
    if payment.status != "pending":
        return None
    if not payment.confirmation_url:
        # Без ссылки напоминание превращается в «заплатите, но не скажу как».
        logger.info("Заказ %s: у платежа нет ссылки, напоминание пропускаем", order.id)
        return None

    deadline = expires_at(order)
    second_due = deadline - timedelta(minutes=settings.payment_reminder_2_before_expiry_minutes)
    first_due = _created_at(order) + timedelta(
        minutes=settings.payment_reminder_1_after_minutes
    )

    if order.reminder_2_sent_at is None and now >= second_due:
        if worktime.is_quiet(now) and worktime.quiet_until(now) >= deadline:
            # К утру ссылка уже не будет работать — вместо напоминания
            # клиент получит новость о закрытии счёта, и это честнее.
            logger.info("Заказ %s: второе напоминание потеряло смысл до утра", order.id)
            await orders_repository.set_state(order.id, reminder_2_sent_at=now)
            return None
        if worktime.is_quiet(now):
            return None
        if await _dialogue_is_live(order):
            return None
        sent = await client_messages.send(
            peer_id=order.peer_id,
            ref=client_messages.order_ref(order.id),
            event_type=templates.REMINDER_2,
            text=templates.reminder_2(order, payment.confirmation_url, deadline),
        )
        await orders_repository.set_state(order.id, reminder_2_sent_at=now)
        return templates.REMINDER_2 if sent else None

    if order.reminder_1_sent_at is None and now >= first_due:
        if worktime.is_quiet(now):
            return None
        if await _dialogue_is_live(order):
            return None
        sent = await client_messages.send(
            peer_id=order.peer_id,
            ref=client_messages.order_ref(order.id),
            event_type=templates.REMINDER_1,
            text=templates.reminder_1(order, payment.confirmation_url),
        )
        await orders_repository.set_state(order.id, reminder_1_sent_at=now)
        return templates.REMINDER_1 if sent else None

    return None


async def check_pending() -> dict:
    """Перечитать у ЮKassa всё, что ещё не досчитано."""
    result = {
        "checked": 0, "paid": 0, "canceled": 0, "unpaid": 0,
        "reminders": 0, "receipts": 0, "failed": 0,
    }

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
                # Разбираем тем же кодом, что и уведомление: причина отмены
                # решает, что сказать клиенту, а закрытие счёта одинаково.
                result["canceled"] += 1
                await webhook._on_canceled(payment)
                continue

            if now >= expires_at(order):
                # Счёт прожил свой срок: закрываем, возвращаем черновик и
                # говорим об этом обоим — клиенту и менеджеру.
                result["unpaid"] += 1
                await payment_service.close_invoice(
                    order, payment, notice=templates.PAYMENT_EXPIRED
                )
                await order_chat.send(
                    order,
                    templates.manager_unpaid(
                        order, payment.status, settings.payment_unpaid_after_hours
                    ),
                )
                continue

            reminded = await _remind(order, payment, now)
            if reminded:
                result["reminders"] += 1
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
