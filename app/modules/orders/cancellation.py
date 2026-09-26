"""Отмена заказа по просьбе клиента — пока денег нет и посылку не заводили.

Раньше инструмента отмены у бота не было, и на «отмените заказ» он звал
менеджера — даже когда отменять было нечего, кроме черновика и неоплаченного
счёта. Менеджеру это лишняя работа, клиенту — ожидание там, где ответ
очевиден.

Что бот отменяет сам: черновик и заказ, счёт по которому не оплачен
(ждёт оплаты, истёк или не выставился). Отправление у перевозчика при
включённой оплате заводится только после денег, так что у такого заказа его
нет. Оплаченный заказ бот не трогает: там возврат денег и, возможно, уже
едущая посылка — это дело менеджера.

Отмена и оплата могут встретиться: закрыть `pending` у ЮKassa по нашему
запросу нельзя, и клиент способен заплатить по старой ссылке уже после
отмены. Поэтому статус ставится условным `UPDATE … WHERE` не оплачен, а
`claim_paid` не берёт отменённый заказ. Кто успел первым, тот и прав: либо
заказ отменён и пришедшие деньги возвращаются автоматически
(`webhook.handle_paid`), либо он оплачен и отмена его уже не трогает.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.core.database import get_session_factory
from app.messages import templates
from app.modules.orders import order_chat, repository as orders_repository, state
from app.modules.orders.models import Order
from app.modules.payment import service as payment_service, yookassa_client

logger = logging.getLogger(__name__)

STATUS_CANCELED = orders_repository.CANCELED

# Заказы, которые клиент может отменить сам: денег нет, отправления нет.
CANCELABLE = (
    payment_service.STATUS_AWAITING_PAYMENT,
    payment_service.STATUS_UNPAID,
    payment_service.STATUS_PAYMENT_FAILED,
)

# Дальше не смотрим: заказ месячной давности клиент вряд ли имеет в виду.
_LOOK_BACK = timedelta(days=30)


@dataclass
class Outcome:
    canceled: list[int] = field(default_factory=list)
    draft_dropped: bool = False
    # Оплаченные и ещё не завершённые — их бот не отменяет.
    paid: list[int] = field(default_factory=list)


async def cancel_for_client(peer_id: int) -> Outcome:
    outcome = Outcome()

    if await state.get_draft(peer_id) is not None:
        await state.clear_draft(peer_id)
        outcome.draft_dropped = True

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        # Без базы заказов нет — отменять, кроме черновика, нечего.
        return outcome

    since = datetime.now(timezone.utc) - _LOOK_BACK
    async with session_factory() as session:
        candidates = (
            await session.execute(
                select(Order.id).where(
                    Order.peer_id == peer_id,
                    Order.status.in_(CANCELABLE),
                    Order.payment_status.is_distinct_from(orders_repository.PAID),
                    Order.created_at > since,
                )
            )
        ).scalars().all()
        outcome.paid = list(
            (
                await session.execute(
                    select(Order.id).where(
                        Order.peer_id == peer_id,
                        Order.payment_status == orders_repository.PAID,
                        Order.status.not_in(("refunded", STATUS_CANCELED)),
                        Order.delivered_at.is_(None),
                        Order.not_delivered_at.is_(None),
                        Order.created_at > since,
                    )
                )
            ).scalars().all()
        )

        canceled: list[Order] = []
        for order_id in candidates:
            # Условие повторяет выборку: между ней и этой строкой могли
            # прийти деньги, и оплаченный заказ отменять уже нельзя.
            order = (
                await session.execute(
                    update(Order)
                    .where(
                        Order.id == order_id,
                        Order.status.in_(CANCELABLE),
                        Order.payment_status.is_distinct_from(orders_repository.PAID),
                    )
                    .values(status=STATUS_CANCELED)
                    .returning(Order)
                )
            ).scalars().first()
            if order is not None:
                canceled.append(order)
        await session.commit()

    for order in canceled:
        outcome.canceled.append(order.id)
        logger.info("Заказ %s отменён по просьбе клиента", order.id)
        await _close_invoices(order)
        await order_chat.send(order, templates.manager_order_canceled(order))

    return outcome


async def _close_invoices(order: Order) -> None:
    """Закрыть счета отменённого заказа у себя и попросить ЮKassa о том же.

    `pending` ЮKassa отменять обычно отказывается и закрывает сама по
    сроку — это штатный исход. Если клиент успеет заплатить, деньги уйдут в
    автоматический возврат: заказ отменён, `claim_paid` его не возьмёт.
    """
    payment_ids = [
        row.payment_id
        for row in await orders_repository.payments_of(order.id)
        if row.closed_at is None
    ]
    if not payment_ids and order.payment_id:
        # Заказ старше таблицы попыток: счёт есть только в самом заказе.
        payment_ids = [order.payment_id]

    for payment_id in payment_ids:
        await orders_repository.close_payment(payment_id)
        try:
            await yookassa_client.cancel_payment(payment_id)
        except Exception as error:
            logger.info(
                "Заказ %s: ЮKassa не отменила счёт %s — %s", order.id, payment_id, error
            )


async def cancel_by_manager(order_id: int, *, paid_too: bool = False) -> dict:
    """Отменить заказ служебной командой — `orders/<N>/cancel`.

    Нужна для хвостов: заказы, «оплаченные» в тестовом магазине ЮKassa,
    числятся оплаченными навсегда — вернуть по ним нечего, а бот видит их
    и на просьбу клиента об отмене зовёт менеджера. Оплаченный заказ
    отменяется только с явным `paid=1`: деньги сами не вернутся и
    отправление у перевозчика не отменится — это делается отдельно.
    Клиенту команда ничего не пишет.
    """
    order = await orders_repository.by_id(order_id)
    if order is None:
        return {"error": f"заказа №{order_id} нет в базе"}
    if order.status in (STATUS_CANCELED, "refunded"):
        return {"заказ": order_id, "действий": f"нет, уже «{order.status}»"}
    is_paid = order.payment_status == orders_repository.PAID
    if is_paid and not paid_too:
        return {
            "заказ": order_id,
            "действий": "нет",
            "почему": (
                "заказ оплачен: команда не вернёт деньги и не отменит "
                "отправление у перевозчика. Если всё равно отменить (например, "
                "это оплата тестового магазина) — добавь ?paid=1"
            ),
            "отправление": order.ozon_posting or order.cdek_uuid or "нет",
        }

    await orders_repository.set_state(order_id, status=STATUS_CANCELED)
    if not is_paid:
        await _close_invoices(order)
    logger.info("Заказ %s отменён служебной командой (оплачен: %s)", order_id, is_paid)
    result = {"заказ": order_id, "отменён": True, "был статус": order.status}
    if is_paid:
        result["помни"] = "деньги сами не вернутся, отправление у перевозчика не отменено"
        result["отправление"] = order.ozon_posting or order.cdek_uuid or "нет"
    return result
