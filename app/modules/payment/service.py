"""Оплата заказа: выставление счёта и статусы.

Пока `PAYMENTS_ENABLED=false`, модуль в работе не участвует — заказ на шаге
оплаты уходит менеджеру эскалацией. Когда флаг поднят, счёт выставляет
ЮKassa, и сюда же приходит ответ на вопрос «оплачено ли».
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.messages import client as client_messages, templates
from app.modules.orders import repository as orders_repository, state
from app.modules.orders.state import OrderDraft
from app.modules.payment import yookassa_client

logger = logging.getLogger(__name__)

# Статусы заказа вокруг оплаты. Живут в той же колонке `status`, что и
# статусы сверки с перевозчиками: заводить вторую колонку ради трёх значений
# незачем, а порядок состояний у заказа всё равно один.
STATUS_AWAITING_PAYMENT = "awaiting_payment"
STATUS_PAID = "paid"
# Оплату ждём не вечно: ссылка живёт ограниченное время, и висящий заказ
# лучше показать менеджеру, чем держать в ожидании бесконечно.
UNPAID_AFTER_HOURS = 24


def is_enabled() -> bool:
    return settings.payments_enabled and yookassa_client.is_configured()


async def create_payment(draft: OrderDraft, order_key: str) -> yookassa_client.Payment:
    """Выставить счёт по черновику. Исключения разбирает вызывающий."""
    details = draft.details
    return await yookassa_client.create_payment(
        order_key=order_key,
        items=draft.items,
        delivery_cost=draft.delivery_cost or 0,
        delivery_label=draft.delivery_label or "",
        email=details.get("recipient_email", ""),
        phone=details.get("recipient_phone", ""),
        full_name=details.get("recipient_name", ""),
        description=f"Заказ {order_key}",
    )


# Статус заказа, счёт по которому закрыт и не оплачен.
STATUS_UNPAID = "payment_expired"

# Что делать с отменённым платежом: три разных разговора с клиентом.
ON_CANCEL_DECLINED = "declined"
ON_CANCEL_EXPIRED = "expired"
ON_CANCEL_BY_MERCHANT = "merchant"


def decide_on_cancel(party: str, reason: str) -> str:
    """Как отвечать на отмену платежа.

    ЮKassa в `cancellation_details` говорит, кто отменил и почему, и эти
    случаи требуют разного. Отказ банка — повод предложить другую карту.
    Истёкший срок клиент уже знает: про закрытие счёта он услышал от нас
    (задача про напоминания), и второе сообщение было бы про то же. Отмену
    со стороны магазина объясняет менеджер — у бота нет причины, которую
    можно назвать клиенту.
    """
    if reason.startswith("expired"):
        return ON_CANCEL_EXPIRED
    if party == "merchant" or reason == "canceled_by_merchant":
        return ON_CANCEL_BY_MERCHANT
    return ON_CANCEL_DECLINED


async def close_invoice(order, payment, *, notice: str | None) -> None:
    """Закрыть счёт и вернуть клиенту черновик на подтверждение.

    Один код на два повода — истёк срок и банк отказал, — потому что для
    заказа это одно и то же: денег нет, счёт не работает, а собранный заказ
    терять незачем. Отличается только сообщение клиенту, и его тип
    передаётся параметром; `None` означает «клиенту не пишем».

    Порядок важен. Сначала статус — чтобы тик, пришедший через пять минут,
    не начал всё заново. Потом отмена платежа у ЮKassa: `pending` она
    отменять обычно отказывается (закрывает сама), и её отказ здесь
    нормальный исход, а не поломка. Потом черновик: состав, доставка и
    получатель сохранились, клиенту достаточно сказать «да».
    """
    await orders_repository.set_state(
        order.id, status=STATUS_UNPAID, payment_status=payment.status
    )

    if payment.status == "pending":
        try:
            await yookassa_client.cancel_payment(order.payment_id)
        except Exception as error:
            # Штатный исход: `pending` ЮKassa закрывает сама и на запрос
            # отвечает отказом. Заказ у нас уже закрыт, и это главное.
            logger.info("Заказ %s: ЮKassa не отменила платёж — %s", order.id, error)

    await restore_draft(order)

    if notice is None:
        return

    text = (
        templates.payment_declined(order)
        if notice == templates.PAYMENT_DECLINED
        else templates.payment_expired(order)
    )
    await client_messages.send(
        peer_id=order.peer_id,
        ref=client_messages.order_ref(order.id),
        event_type=notice,
        text=text,
    )


async def restore_draft(order) -> None:
    """Вернуть черновик на подтверждение, ничего не потеряв.

    Номер заказа из деталей убираем намеренно: из него выводится ключ
    идемпотентности ЮKassa, и с прежним номером повторное подтверждение
    вернуло бы тот же — уже закрытый — платёж вместо нового счёта.

    Если клиент успел собрать новый заказ, его черновик не трогаем: он
    важнее старого.
    """
    existing = await state.get_draft(order.peer_id)
    if existing is not None:
        logger.info("Заказ %s: у клиента уже есть черновик, старый не возвращаем", order.id)
        return

    details = dict(order.details or {})
    details.pop("order_key", None)
    await state.set_draft(
        order.peer_id,
        OrderDraft(
            items=list(order.items or []),
            items_total=float(order.items_total or 0),
            delivery_method=order.delivery_method,
            delivery_label=details.get("delivery_label") or order.delivery_method or None,
            delivery_cost=float(order.delivery_cost) if order.delivery_cost is not None else None,
            details=details,
            stage="awaiting_confirmation",
        ),
    )


async def generate_payment_link(draft: OrderDraft) -> str:
    """Оставлено ради обратной совместимости со старым вызовом.

    Настоящее выставление счёта идёт через `create_payment`: ему нужен номер
    заказа для ключа идемпотентности, а по одному черновику его не собрать.
    """
    return (
        "Ссылка на оплату скоро будет готова — модуль оплаты ещё не "
        "подключён, менеджер свяжется с вами для оформления."
    )
