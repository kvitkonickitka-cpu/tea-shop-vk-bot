"""Уведомления ЮKassa о смене статуса платежа.

Подлинность уведомления **не проверяется по телу**. Адрес эндпоинта открыт
всему интернету, токен ЮKassa передать не может, а поддельное уведомление
стоит дешево. Поэтому из тела берётся только идентификатор платежа, а его
состояние спрашивается у ЮKassa отдельным запросом — так документация и
советует. Подделка в худшем случае заставит нас перечитать настоящий платёж
и подтвердить то, что и так верно.

Проверку по списку адресов ЮKassa не делаем: запрос приходит через шлюз
Yandex Cloud, исходный адрес пришлось бы доставать из заголовков, которым
доверия не больше, чем телу.
"""

from __future__ import annotations

import logging

from app.messages import client as client_messages, templates
from app.modules.orders import (
    cdek_watch,
    order_chat,
    repository as orders_repository,
    shipping,
)
from app.modules.payment import service as payment_service, yookassa_client

logger = logging.getLogger(__name__)

# События, на которые подписываемся в кабинете. Остальные игнорируем молча:
# отвечать ошибкой на неизвестное событие значит получать его сутки подряд.
EVENT_SUCCEEDED = "payment.succeeded"
EVENT_CANCELED = "payment.canceled"
EVENT_REFUNDED = "refund.succeeded"

# Статус заказа, по которому деньги вернули.
STATUS_REFUNDED = "refunded"


async def handle(body: dict) -> dict:
    """Разобрать уведомление. Исключения наружу — чтобы ЮKassa повторила."""
    event = str(body.get("event") or "")
    object_id = str(((body.get("object") or {}).get("id")) or "")

    # События возврата приносят объект ВОЗВРАТА, а не платежа: его
    # идентификатор нельзя спрашивать у `/payments/…` — будет 404, ответ
    # ошибкой и повтор уведомления сутки подряд.
    if event.startswith("refund."):
        return await _on_refund(object_id)

    payment_id = object_id
    if not payment_id:
        logger.warning("Уведомление ЮKassa без идентификатора платежа: %s", str(body)[:200])
        return {"ignored": "нет идентификатора платежа"}

    # Состояние берём у ЮKassa, а не из тела уведомления.
    payment = await yookassa_client.get_payment(payment_id)
    logger.info(
        "Уведомление ЮKassa: событие %s, платёж %s, статус %s, чек %s",
        event, payment_id, payment.status, payment.receipt_registration or "—",
    )

    if payment.status == "succeeded" and payment.paid:
        return await handle_paid(payment)

    if payment.status == "canceled":
        return await _on_canceled(payment)

    if payment.status == "waiting_for_capture":
        # Такого быть не должно: платежи создаются с `capture: true`, то есть
        # списываются сразу. Если всё же случилось — деньги заморожены у
        # клиента и сами не спишутся, о чём менеджер должен узнать.
        logger.warning(
            "Платёж %s ждёт подтверждения, хотя создавался с capture=true — "
            "деньги заморожены, нужен ручной разбор",
            payment_id,
        )
        return {"платёж": payment_id, "статус": payment.status, "действий": "нужен разбор"}

    # pending: ждать нечего, придёт следующее уведомление. Отвечаем успехом,
    # иначе ЮKassa будет повторять это сутки.
    return {"платёж": payment_id, "статус": payment.status, "действий": "нет"}


async def _on_canceled(payment: yookassa_client.Payment) -> dict:
    """Платёж отменён. Что сказать клиенту — зависит от того, кто отменил.

    ЮKassa кладёт это в `cancellation_details`. Отказ банка — повод
    предложить другую карту; истёкший срок клиент уже знает от нас, и второе
    сообщение было бы про то же; отмену магазином объясняет менеджер.
    """
    order = await orders_repository.by_payment(payment.id)
    if order is None:
        return {"платёж": payment.id, "действий": "нет, заказа не нашли"}

    decision = payment_service.decide_on_cancel(
        payment.cancellation_party, payment.cancellation_reason
    )
    logger.info(
        "Платёж %s отменён: кто «%s», почему «%s» — решение «%s»",
        payment.id, payment.cancellation_party or "—",
        payment.cancellation_reason or "—", decision,
    )

    if order.status == payment_service.STATUS_UNPAID:
        # Счёт уже закрыт — например, догляд успел раньше уведомления.
        return {"платёж": payment.id, "действий": "нет, счёт уже закрыт"}

    notice = templates.PAYMENT_DECLINED if decision == payment_service.ON_CANCEL_DECLINED else None
    await payment_service.close_invoice(order, payment, notice=notice)
    return {"платёж": payment.id, "статус": payment.status, "решение": decision}


async def handle_paid(payment: yookassa_client.Payment) -> dict:
    """Деньги пришли: заводим отправление и показываем заказ менеджеру."""
    order = await orders_repository.claim_paid(
        payment.id, payment.status, payment.receipt_registration
    )
    if order is None:
        # Либо заказа с таким платежом у нас нет (чужое или поддельное
        # уведомление), либо его уже обработали — повтор от ЮKassa.
        logger.info("Платёж %s: заказ не найден или уже обработан", payment.id)
        return {"платёж": payment.id, "действий": "нет, уже обработан"}

    logger.info("Заказ %s оплачен, заводим отправление", order.id)
    registered = await shipping.register(
        peer_id=order.peer_id,
        delivery_method=order.delivery_method,
        items=order.items or [],
        details=order.details or {},
        items_total=float(order.items_total or 0),
        delivery_cost=float(order.delivery_cost or 0),
        order_key=(order.details or {}).get("order_key", ""),
    )

    fields = {}
    if registered.cdek_uuid:
        fields["cdek_uuid"] = registered.cdek_uuid
        # СДЭК отвечает на создание заявки «принял», а состоялся ли заказ,
        # выясняет сверка по таймеру — она и узнаёт номер накладной, и
        # замечает отказ. Ищет она заказы в этом статусе, поэтому оплаченный
        # заказ надо ей вернуть: со статусом «оплачен» он не проверялся
        # вовсе, и накладная не появлялась ни у клиента, ни в чате заказов.
        fields["status"] = cdek_watch.STATUS_NEW
    if registered.ozon_posting:
        fields["ozon_posting"] = registered.ozon_posting
    if fields:
        await orders_repository.set_state(order.id, **fields)
        # Чтобы карточка в чате показала накладную, а не пустое место.
        order.cdek_uuid = registered.cdek_uuid or order.cdek_uuid
        order.ozon_posting = registered.ozon_posting or order.ozon_posting

    await order_chat.send(order, _paid_card(order, payment))
    await _tell_client(order, registered)
    return {
        "платёж": payment.id,
        "заказ": order.id,
        "отправление": registered.cdek_uuid or registered.ozon_posting or "не заведено",
    }


async def _tell_client(order, registered: shipping.Registered) -> None:
    """Сказать клиенту, что деньги дошли.

    Раньше об оплате узнавал только менеджер: карточка уходила в телеграм, а
    клиент оставался с ссылкой на оплату и тишиной — заплатил и не знает,
    увидели ли это.
    """
    details = order.details or {}
    await client_messages.send(
        peer_id=order.peer_id,
        ref=client_messages.order_ref(order.id),
        event_type=templates.PAID,
        text=templates.paid(
            order,
            email=details.get("recipient_email", ""),
            phone=details.get("recipient_phone", ""),
            posting=registered.ozon_posting or "",
            cdek=bool(registered.cdek_uuid),
        ),
        on_failure=_client_unreachable(order, templates.PAID),
    )


def _client_unreachable(order, event_type: str):
    """Обработчик отказа ВК: клиент новость не получил, скажем менеджеру."""

    async def report(error: str) -> None:
        await order_chat.send(
            order, templates.manager_client_unreachable(order, event_type, error)
        )

    return report


async def _on_refund(refund_id: str) -> dict:
    """Менеджер вернул деньги клиенту — отметить заказ и сказать в чат."""
    if not refund_id:
        return {"ignored": "нет идентификатора возврата"}

    refund = await yookassa_client.get_refund(refund_id)
    logger.info(
        "Возврат %s по платежу %s: статус %s, сумма %s",
        refund.id, refund.payment_id, refund.status, refund.amount,
    )
    if refund.status != "succeeded" or not refund.payment_id:
        return {"возврат": refund.id, "статус": refund.status, "действий": "нет"}

    order = await orders_repository.by_payment(refund.payment_id)
    if order is None:
        logger.info("Возврат %s: заказа с таким платежом у нас нет", refund.id)
        return {"возврат": refund.id, "действий": "нет, заказ не найден"}

    if order.status == STATUS_REFUNDED:
        return {"возврат": refund.id, "действий": "нет, уже отмечен"}

    await orders_repository.set_state(order.id, status=STATUS_REFUNDED)
    await order_chat.send(
        order,
        f"↩️ <b>Возврат {refund.amount} руб</b>\n" + order_chat.card(order),
    )
    # Возврат делает менеджер в кабинете ЮKassa, и клиент об этом узнаёт
    # только от банка — через неизвестно сколько. Скажем сами. Сумма — та,
    # что вернули: возврат бывает частичным, и сумма заказа тут соврала бы.
    await client_messages.send(
        peer_id=order.peer_id,
        ref=client_messages.order_ref(order.id),
        event_type=templates.REFUNDED,
        text=templates.refunded(order, refund.amount),
        on_failure=_client_unreachable(order, templates.REFUNDED),
    )
    return {"возврат": refund.id, "заказ": order.id}


def _paid_card(order, payment: yookassa_client.Payment) -> str:
    card = "💰 <b>Оплачено</b>\n" + order_chat.card(order)
    if payment.receipt_registration and payment.receipt_registration != "succeeded":
        # Чек регистрирует касса с ОФД, уже после платежа. Пока не
        # зарегистрирован — это не повод дёргать клиента, но менеджер должен
        # видеть, что чека ещё нет.
        card += f"\nЧек: {payment.receipt_registration}"
    return card
