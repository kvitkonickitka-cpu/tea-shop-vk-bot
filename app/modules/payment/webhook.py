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

from app.modules.dialog import vk_client
from app.modules.orders import (
    cdek_watch,
    order_chat,
    repository as orders_repository,
    shipping,
)
from app.modules.payment import yookassa_client

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
        order = await orders_repository.by_payment(payment_id)
        if order is not None:
            await orders_repository.set_state(
                order.id, status="payment_canceled", payment_status=payment.status
            )
        return {"платёж": payment_id, "статус": payment.status}

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


def client_message(order, registered: shipping.Registered) -> str:
    """Что клиент получает в ВК, когда оплата прошла.

    Текст собираем здесь и не зовём Claude: клиента в диалоге в этот момент
    нет — уведомление приходит от ЮKassa, когда он уже ушёл из переписки, — и
    формулировать тут нечего, все данные известны.
    """
    # :g — чтобы в сообщении клиенту не было «917.0 руб».
    lines = ["✅ Оплата получена, спасибо!", f"Заказ №{order.id} на {order.total:g} руб."]

    email = (order.details or {}).get("recipient_email")
    if email:
        lines.append(f"Чек придёт на {email}.")

    if registered.ozon_posting:
        # Клиенту важно не столько само отправление, сколько что делать
        # дальше: номер он увидит в приложении Ozon и там же будет следить
        # за доставкой, без нас и без менеджера.
        lines.append(
            f"Отправление Ozon: {registered.ozon_posting} — по нему посылку видно "
            "в приложении и на сайте Ozon, там же отслеживается доставка."
        )
    elif registered.cdek_uuid:
        lines.append(
            "Передаём посылку в СДЭК. Трек-номер пришлём сюда, как только "
            "СДЭК его выдаст."
        )
    else:
        # Перевозчик не принял отправление (или заказ вообще без него).
        # Пугать клиента нечем: менеджера мы уже предупредили.
        lines.append("Заказ передан в работу, менеджер свяжется с вами по отправке.")

    return "\n".join(lines)


async def _tell_client(order, registered: shipping.Registered) -> None:
    """Сказать клиенту, что деньги дошли.

    Раньше об оплате узнавал только менеджер: карточка уходила в телеграм, а
    клиент оставался с ссылкой на оплату и тишиной — заплатил и не знает,
    увидели ли это.
    """
    try:
        await vk_client.send_message(order.peer_id, client_message(order, registered))
    except Exception:
        # Отправление уже заведено, и заказ оплачен: молчать об ошибке нельзя,
        # но и повторять всю обработку из-за неё тоже — ЮKassa получит 200.
        logger.exception("Не сказали клиенту про оплату заказа %s", order.id)


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
    return {"возврат": refund.id, "заказ": order.id}


def _paid_card(order, payment: yookassa_client.Payment) -> str:
    card = "💰 <b>Оплачено</b>\n" + order_chat.card(order)
    if payment.receipt_registration and payment.receipt_registration != "succeeded":
        # Чек регистрирует касса с ОФД, уже после платежа. Пока не
        # зарегистрирован — это не повод дёргать клиента, но менеджер должен
        # видеть, что чека ещё нет.
        card += f"\nЧек: {payment.receipt_registration}"
    return card
