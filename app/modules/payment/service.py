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


async def create_payment(
    draft: OrderDraft, order_key: str, attempt: int = 1
) -> yookassa_client.Payment:
    """Выставить счёт по черновику. Исключения разбирает вызывающий."""
    details = draft.details
    return await yookassa_client.create_payment(
        order_key=order_key,
        attempt=attempt,
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
        # Отметка «закрыт» ставится у себя всегда, а отмену у ЮKassa она
        # обычно отклоняет: pending закрывает сама и не по нашим часам.
        # Поэтому счёт какое-то время ещё принимает оплату — и если клиент
        # заплатит, `webhook.handle_paid` эти деньги примет, а не потеряет.
        await orders_repository.close_payment(order.payment_id)
        try:
            await yookassa_client.cancel_payment(order.payment_id)
        except Exception as error:
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

    Номер заказа остаётся тем же: клиент видел его в переписке, и менять
    номер из-за неудачной оплаты незачем. Новую попытку различает номер
    попытки — из него выводится ключ идемпотентности ЮKassa, и повторное
    подтверждение создаёт новый платёж, а не возвращает прежний.

    Если клиент успел собрать новый заказ, его черновик не трогаем: он
    важнее старого.
    """
    existing = await state.get_draft(order.peer_id)
    if existing is not None:
        logger.info("Заказ %s: у клиента уже есть черновик, старый не возвращаем", order.id)
        return

    details = dict(order.details or {})
    # Номер заказа и его ключ сохраняем: при повторном подтверждении это
    # тот же заказ, просто вторая попытка оплаты. Новым будет только ключ
    # идемпотентности — он выводится из номера попытки.
    details["order_id"] = order.id
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


# Статус заказа, по которому счёт выставить не удалось. Заказ при этом
# сохраняется: менеджеру нужен номер, чтобы выставить счёт вручную.
STATUS_PAYMENT_FAILED = "payment_failed"


def missing_for_invoice(
    *, items: list, details: dict, delivery_method: str | None
) -> list[str]:
    """Чего не хватает, чтобы выставить счёт. Пусто — всё на месте.

    Те же требования, что и у `confirm_order`: без них платёж либо не
    создастся, либо создастся по неполному заказу, который потом некуда
    отправить.
    """
    details = details or {}
    missing = []
    if not items:
        missing.append("состав заказа")
    if not details.get("recipient_name"):
        missing.append("ФИО получателя")
    phone = details.get("recipient_phone", "")
    if not phone:
        missing.append("телефон получателя")
    elif not yookassa_client.phone_is_valid(phone):
        missing.append(f"телефон получателя целиком (сейчас «{phone}»)")
    if not delivery_method:
        missing.append("способ доставки")
    if delivery_method == "cdek_pvz" and not details.get("delivery_point"):
        missing.append("код пункта выдачи СДЭК")
    if delivery_method == "ozon_pvz" and not details.get("ozon_point_id"):
        missing.append("пункт выдачи Ozon")
    return missing


async def issue_for_order(order) -> tuple[yookassa_client.Payment | None, str]:
    """Выставить счёт по уже сохранённому заказу.

    Нужно менеджеру: заказ есть, а ссылки нет — счёт не создался, истёк или
    клиент просит новую. Код тот же, что и в диалоге: те же проверки
    полноты, тот же `create_payment`, та же запись попытки. Отличается
    только источник данных — заказ, а не черновик.

    Возвращает платёж и пояснение; при отказе платёж пустой.
    """
    from app.modules.orders import repository as orders_repository

    if not is_enabled():
        return None, "оплата не подключена: нет флага PAYMENTS_ENABLED или ключей ЮKassa"
    if order.payment_status == "succeeded":
        return None, "заказ уже оплачен — второй счёт выставлять нельзя"

    details = dict(order.details or {})
    missing = missing_for_invoice(
        items=list(order.items or []),
        details=details,
        delivery_method=order.delivery_method,
    )
    if missing:
        return None, "не хватает: " + ", ".join(missing)

    order_key = details.get("order_key") or f"order{order.id}"
    details["order_key"] = order_key
    draft = OrderDraft(
        items=list(order.items or []),
        items_total=float(order.items_total or 0),
        delivery_method=order.delivery_method,
        delivery_label=details.get("delivery_label") or order.delivery_method,
        delivery_cost=float(order.delivery_cost) if order.delivery_cost is not None else None,
        details=details,
        stage="awaiting_confirmation",
    )

    attempt = await orders_repository.next_attempt(order.id)
    payment = await create_payment(draft, order_key, attempt)

    await orders_repository.reopen_for_payment(order.id, draft, payment.id, payment.status)
    await orders_repository.register_payment(
        order.id, payment.id, attempt=attempt, status=payment.status, amount=payment.amount
    )
    return payment, f"счёт выставлен, попытка {attempt}"


async def generate_payment_link(draft: OrderDraft) -> str:
    """Оставлено ради обратной совместимости со старым вызовом.

    Настоящее выставление счёта идёт через `create_payment`: ему нужен номер
    заказа для ключа идемпотентности, а по одному черновику его не собрать.
    """
    return (
        "Ссылка на оплату скоро будет готова — модуль оплаты ещё не "
        "подключён, менеджер свяжется с вами для оформления."
    )
