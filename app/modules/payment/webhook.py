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
from app.modules.marking import packing
from app.modules.orders import (
    cdek_watch,
    order_chat,
    repository as orders_repository,
    shipping,
)
from app.modules.payment import service as payment_service, settlement, yookassa_client

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
    if order.status == orders_repository.CANCELED:
        # Клиент отменил заказ сам. Закрытие счёта вернуло бы черновик на
        # подтверждение и написало бы «пришлю новую ссылку» — то есть
        # воскресило бы отменённый заказ.
        return {"платёж": payment.id, "действий": "нет, заказ отменён клиентом"}

    notice = (
        templates.PAYMENT_DECLINED
        if decision == payment_service.ON_CANCEL_DECLINED
        else templates.PAYMENT_EXPIRED
        if decision == payment_service.ON_CANCEL_EXPIRED
        else None  # отмена магазином: причину объясняет менеджер
    )
    await payment_service.close_invoice(order, payment, notice=notice)
    return {"платёж": payment.id, "статус": payment.status, "решение": decision}


async def handle_paid(payment: yookassa_client.Payment) -> dict:
    """Деньги пришли: заводим отправление и показываем заказ менеджеру.

    Платёж может оказаться **старым** — по счёту, который мы уже закрыли.
    Отменить pending у ЮKassa нельзя, она закрывает его сама и не сразу, так
    что клиент вполне может заплатить по прежней ссылке. Если заказ ещё не
    оплачен, такие деньги принимаются как обычные: они настоящие.

    А вот вторые деньги по уже оплаченному заказу принимать нельзя — их
    возвращаем сами, не дожидаясь менеджера.
    """
    known = await orders_repository.by_payment(payment.id)
    if known is None:
        logger.info("Платёж %s: заказа с таким платежом у нас нет", payment.id)
        return {"платёж": payment.id, "действий": "нет, заказа не нашли"}

    # Был ли этот счёт уже закрыт с нашей стороны — нужно менеджеру: заказ
    # он видел закрытым, а деньги пришли.
    attempt_row = await orders_repository.payment_of(payment.id)
    was_closed = bool(attempt_row and attempt_row.closed_at)

    order = await orders_repository.claim_paid(
        payment.id, payment.status, payment.receipt_registration, order_id=known.id
    )
    if order is None:
        # Заказ уже оплачен. Либо это повтор уведомления по тому же платежу
        # (ничего не делаем), либо деньги пришли вторым платежом — и тогда
        # их надо вернуть.
        fresh = await orders_repository.by_payment(payment.id)
        if (
            fresh is not None
            and fresh.status == orders_repository.CANCELED
            and fresh.payment_status != orders_repository.PAID
        ):
            # Клиент отменил заказ, а потом заплатил по старой ссылке:
            # отправление не заводим, деньги возвращаем.
            if attempt_row is not None and attempt_row.refund_id:
                return {"платёж": payment.id, "действий": "нет, уже возвращён"}
            return await _refund_whole(fresh, payment, canceled=True)
        if fresh is not None and fresh.payment_id != payment.id:
            return await _refund_whole(fresh, payment)
        logger.info("Платёж %s: заказ уже обработан", payment.id)
        return {"платёж": payment.id, "действий": "нет, уже обработан"}

    # Остальные счёта по этому заказу больше не наши: пометим закрытыми,
    # чтобы оплата по ним попала в ветку возврата, а не завела вторую
    # посылку. Попытку отмены ЮKassa для pending обычно отклоняет.
    await _close_other_payments(order.id, payment.id)

    logger.info(
        "Заказ %s оплачен%s, заводим отправление",
        order.id, " по закрытому счёту" if was_closed else "",
    )
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

    await order_chat.send(order, _paid_card(order, payment, was_closed=was_closed))
    await _tell_client(order, registered)
    return {
        "платёж": payment.id,
        "заказ": order.id,
        "оплачен закрытый счёт": was_closed,
        "отправление": registered.cdek_uuid or registered.ozon_posting or "не заведено",
    }


async def _close_other_payments(order_id: int, paid_payment_id: str) -> None:
    """Закрыть прочие счёта заказа: оплачен один, остальные не нужны."""
    for row in await orders_repository.payments_of(order_id):
        if row.payment_id == paid_payment_id or row.closed_at is not None:
            continue
        await orders_repository.close_payment(row.payment_id)
        try:
            await yookassa_client.cancel_payment(row.payment_id)
        except Exception as error:
            # Ожидаемо: pending ЮKassa отменять отказывается. Отметка у нас
            # уже стоит, и оплата по такому счёту уйдёт в возврат.
            logger.info("Счёт %s ЮKassa не отменила — %s", row.payment_id, error)


async def _refund_whole(
    order, payment: yookassa_client.Payment, *, canceled: bool = False
) -> dict:
    """Вернуть деньги, которые принять нельзя, и сказать об этом.

    Два повода, и оба от того, что закрыть pending по нашему запросу ЮKassa
    не даёт. Клиент заплатил дважды — по старой ссылке уже после новой. Или
    клиент отменил заказ, а потом всё же заплатил по старой ссылке
    (`canceled=True`). Держать такие деньги у себя нельзя, и ждать с этим
    менеджера тоже: возврат делается сразу, с чеком.
    """
    details = order.details or {}
    logger.warning(
        "Заказ %s: пришла %s платежом %s на %s руб — возвращаем",
        order.id, "оплата отменённого заказа" if canceled else "вторая оплата",
        payment.id, payment.amount,
    )
    if canceled:
        stuck_card, stuck_type, stuck_text = (
            templates.manager_canceled_paid_stuck,
            templates.CANCELED_PAID_STUCK,
            templates.canceled_paid_stuck,
        )
        done_card, done_type, done_text = (
            templates.manager_canceled_paid,
            templates.CANCELED_PAID,
            templates.canceled_paid,
        )
    else:
        stuck_card, stuck_type, stuck_text = (
            templates.manager_double_payment_stuck,
            templates.DOUBLE_PAYMENT_STUCK,
            templates.double_payment_stuck,
        )
        done_card, done_type, done_text = (
            templates.manager_double_payment,
            templates.DOUBLE_PAYMENT,
            templates.double_payment,
        )

    try:
        refund = await yookassa_client.create_refund(
            payment_id=payment.id,
            amount=payment.amount,
            items=order.items or [],
            delivery_cost=float(order.delivery_cost or 0),
            delivery_label=order.delivery_method or "",
            email=details.get("recipient_email", ""),
            phone=details.get("recipient_phone", ""),
            full_name=details.get("recipient_name", ""),
            # Вторая оплата возвращается целиком — чек возврата ЮKassa
            # соберёт сама по чеку этого платежа.
            full=True,
        )
    except Exception as error:
        logger.exception("Не вернули оплату %s по заказу %s", payment.id, order.id)
        await order_chat.send(order, stuck_card(order, payment, str(error)[:300]))
        await client_messages.send(
            peer_id=order.peer_id,
            ref=f"{client_messages.order_ref(order.id)}:{payment.id}",
            event_type=stuck_type,
            text=stuck_text(order),
        )
        return {"платёж": payment.id, "действий": "возврат не удался"}

    # Помечаем возврат у попытки: по этой отметке обработчик `refund.*`
    # поймёт, что возврат наш, и не станет объявлять заказ возвращённым:
    # либо заказ оплачен и посылка едет, либо он отменён и сказано уже всё.
    await orders_repository.close_payment(payment.id, refund_id=refund.id)

    await order_chat.send(order, done_card(order, payment, refund))
    await client_messages.send(
        peer_id=order.peer_id,
        ref=f"{client_messages.order_ref(order.id)}:{payment.id}",
        event_type=done_type,
        text=done_text(order, payment.amount),
    )
    return {
        "платёж": payment.id,
        "заказ": order.id,
        "возврат": refund.id,
        "статус возврата": refund.status,
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

    # Наш собственный возврат второй оплаты: заказ при этом оплачен и
    # посылка едет. Объявить его возвращённым значило бы остановить
    # доставку и сказать клиенту про возврат дважды.
    attempt_row = await orders_repository.payment_of(refund.payment_id)
    if attempt_row is not None and attempt_row.refund_id == refund.id:
        logger.info("Возврат %s — наш, автоматический, по заказу %s", refund.id, order.id)
        return {"возврат": refund.id, "действий": "нет, это возврат второй оплаты"}

    if order.status == STATUS_REFUNDED:
        return {"возврат": refund.id, "действий": "нет, уже отмечен"}

    await orders_repository.set_state(order.id, status=STATUS_REFUNDED)
    card = f"↩️ <b>Возврат {refund.amount} руб</b>\n" + order_chat.card(order)
    if order.delivered_at is None:
        # Посылка не вручена — пачки не проданы и вернутся на полку (или не
        # уезжали вовсе). Коды снова в наличии, закрывающий чек не нужен.
        released = await packing.release_codes(order.id)
        if released:
            card += "\n" + templates.manager_refund_codes_released(released)
    elif settlement.was_sent(order):
        # Товар уже продан по чеку с кодами. Чек возврата, который ЮKassa
        # соберёт по данным платежа, — предоплата без кодов — для такого
        # случая неверен. Автоматики на это нет: разбирает менеджер.
        card += "\n" + templates.manager_refund_after_settlement()
    await order_chat.send(order, card)
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


def _paid_card(order, payment: yookassa_client.Payment, *, was_closed: bool = False) -> str:
    head = "💰 <b>Оплачено</b>"
    if was_closed:
        # Менеджер видел этот заказ закрытым: деньги пришли по счёту, про
        # который мы клиенту уже сказали «срок истёк».
        head = "💰 <b>Оплачено — по закрытому счёту</b>"
    card = head + "\n" + order_chat.card(order)
    if payment.receipt_registration and payment.receipt_registration != "succeeded":
        # Чек регистрирует касса с ОФД, уже после платежа. Пока не
        # зарегистрирован — это не повод дёргать клиента, но менеджер должен
        # видеть, что чека ещё нет.
        card += f"\nЧек: {payment.receipt_registration}"
    # Ссылка на сборку со сканированием кодов маркировки — если у товаров
    # заданы GTIN и известен адрес контейнера.
    pack_line = packing.card_line(order)
    if pack_line:
        card += "\n" + pack_line
    return card
