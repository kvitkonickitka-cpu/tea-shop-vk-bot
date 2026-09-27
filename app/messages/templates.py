"""Все шаблоны сообщений: клиенту в ВК и менеджеру в телеграм.

Одним файлом намеренно. Раньше тексты жили там, где отправлялись — в
уведомлении ЮKassa, в догляде за платежами, в сверке СДЭКа, — и вычитать
их целиком было невозможно: чтобы понять, что вообще получает клиент,
приходилось читать пять модулей.

Правила, по которым они написаны:

- клиент получает следствие и что делать дальше, а не диагностику. Код
  ошибки перевозчика, статус чека у ЮKassa и номер заявки — менеджеру;
- сумма без хвоста «.0» и со знаком рубля: 917 ₽, а не 917.0 руб.;
- никаких обещаний, которых мы не контролируем: срок зачисления возврата
  зависит от банка, и так и написано.
"""

from __future__ import annotations

from app.core import worktime
from app.core.config import settings

# Типы событий: они же ключи журнала отправок, поэтому строки постоянные.
PAID = "paid"
CDEK_TRACK = "cdek_track"
SHIPMENT_TROUBLE = "shipment_trouble"
PAYMENT_DECLINED = "payment_declined"
PAYMENT_EXPIRED = "payment_expired"
REFUNDED = "refunded"
RECEIPT_DELAYED = "receipt_delayed"
REMINDER_1 = "payment_reminder_1"
REMINDER_2 = "payment_reminder_2"
ESCALATION_WAITING = "escalation_waiting"
DOUBLE_PAYMENT = "double_payment"
DOUBLE_PAYMENT_STUCK = "double_payment_stuck"
CANCELED_PAID = "canceled_paid"
CANCELED_PAID_STUCK = "canceled_paid_stuck"
HANDED_OVER = "handed_over"
DELIVERED = "delivered"
NOT_DELIVERED = "not_delivered"
DRAFT_NUDGE = "draft_nudge_sent"
AT_PICKUP = "at_pickup_point"
PICKUP_EXPIRING = "pickup_expiring"


def amount(value) -> str:
    """Сумма без лишнего нуля после точки."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def day_month(day) -> str:
    """«5 октября» — дата без года: срок хранения всегда в ближайшие дни."""
    return f"{day.day} {_MONTHS[day.month - 1]}"


def composition(items: list[dict]) -> str:
    """Состав для текста клиенту: «Те Гуань Инь × 2, Да Хун Пао»."""
    parts = []
    for item in items or []:
        quantity = item.get("quantity", 1)
        name = item.get("name", "товар")
        parts.append(f"{name} × {quantity}" if quantity and quantity != 1 else name)
    return ", ".join(parts)


def mask_phone(raw: str) -> str:
    """Телефон для показа клиенту: +7 *** ***-00-00.

    Полный номер в сообщении не нужен — клиент и так его знает, — а
    переписка попадает и в скриншоты, и в отчёты.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) < 4:
        return "ваш номер"
    return f"+{digits[0]} *** ***-{digits[-4:-2]}-{digits[-2:]}"


def receipt_destination(email: str, phone: str) -> str:
    """Куда придёт чек. Почта, если её дали, иначе номер телефона."""
    if email:
        return email
    if phone:
        return f"номер {mask_phone(phone)}"
    return "указанные вами контакты"


# --- клиенту ---------------------------------------------------------------


def paid(order, *, email: str = "", phone: str = "", posting: str = "", cdek: bool = False) -> str:
    """Оплата получена. Посылка в этот момент ещё не собрана — так и говорим.

    Раньше номер отправления Ozon подавался так, будто посылка уже едет, а
    СДЭКу «передаём посылку» писалось, когда её никто ещё не собирал.
    """
    lines = [
        "✅ Оплата получена, спасибо!",
        f"Заказ №{order.id} на {amount(order.total)} ₽.",
        f"Чек придёт на {receipt_destination(email, phone)}.",
    ]
    promise = settings.handover_promise
    if posting:
        lines.append(
            f"Соберём посылку и сдадим в Ozon {promise}. Номер отправления: "
            f"{posting}. Напишем, когда посылка поедет и когда приедет в пункт выдачи."
        )
    elif cdek:
        lines.append(
            f"Соберём посылку и сдадим в СДЭК {promise}. Трек-номер пришлём сюда, "
            "как только оформим отправление."
        )
    else:
        lines.append(
            "Заказ в работе. Для передачи в доставку нужно участие менеджера — "
            "он напишет вам здесь."
        )
    return "\n".join(lines)


def invoice_ready(*, total, link: str, email: str = "", phone: str = "") -> str:
    """Счёт выставлен — ответ на «да» в диалоге.

    Не «заказ оформлен»: до оплаты клиент читал это как «всё готово». Сумма
    рядом со ссылкой снимает вопрос «а сколько там», срок ссылки задаёт
    ожидание, ссылка на условия — то, что покупатель принимает, когда платит.
    """
    lines = [
        f"Счёт на {amount(total)} ₽ готов: {link}",
        f"Ссылка действует {settings.payment_invoice_ttl_minutes} минут. После "
        f"оплаты пришлём чек на {receipt_destination(email, phone)} и сразу "
        "передадим заказ в доставку.",
    ]
    if settings.conditions_url:
        lines.append(f"Условия покупки, доставки и возврата: {settings.conditions_url}")
    return "\n".join(lines)


def cdek_track(order, number: str, tracking_url: str) -> str:
    # Накладная — это оформление, а не передача: СДЭК выдаёт номер через
    # минуты после оплаты, а посылка в это время лежит у нас. Прежнее
    # «передана в СДЭК» противоречило треку со статусом «заказ создан».
    return (
        f"Оформили отправление в СДЭК по заказу №{order.id}.\n"
        f"Трек-номер: {number}\n"
        "Пока по треку будет статус «заказ создан» — посылку ещё собираем. "
        "Напишем, когда сдадим её в СДЭК.\n"
        f"Отследить: {tracking_url}"
    )


def shipment_trouble(order) -> str:
    """Оплаченный заказ, который перевозчик не принял."""
    return (
        f"Заказ №{order.id} оплачен, с передачей в доставку возникла заминка.\n"
        f"Деньги в сохранности, менеджер свяжется с вами "
        f"{worktime.working_day_phrase()}."
    )


def payment_declined(order) -> str:
    """Банк или платёжная система отказали."""
    return (
        f"Платёж по заказу №{order.id} на {amount(order.total)} ₽ не прошёл. Если "
        "банк успел заблокировать сумму на карте, она вернётся автоматически.\n"
        "Можно попробовать ещё раз, другой картой или другим способом — напишите "
        "сюда, пришлю новую ссылку 🙏"
    )


def payment_expired(order) -> str:
    """Срок счёта истёк.

    Про ссылку намеренно не говорим «перестала работать»: отменить
    pending-платёж у ЮKassa нельзя, она закрывает его сама и не сразу — то
    есть ссылка какое-то время ещё принимает оплату. Обещать обратное
    значит врать; если клиент всё же заплатит по ней, оплату мы примем.
    """
    return (
        f"Срок счёта по заказу №{order.id} истёк.\n"
        "Если заказ актуален, напишите сюда: пришлю новую ссылку, состав и "
        "доставка сохранились."
    )


def double_payment(order, refunded_amount) -> str:
    return (
        f"По заказу №{order.id} пришла повторная оплата — вернули "
        f"{amount(refunded_amount)} ₽.\n"
        "Заказ оплачен один раз и уже в работе, ничего делать не нужно. Срок "
        "зачисления возврата зависит от банка, чек возврата придёт на ту же почту."
    )


def double_payment_stuck(order) -> str:
    return (
        f"По заказу №{order.id} пришла повторная оплата.\n"
        "Разбираемся с возвратом — менеджер свяжется с вами "
        f"{worktime.working_day_phrase()}. Заказ оплачен один раз и уже в работе."
    )


def canceled_paid(order, refunded_amount) -> str:
    return (
        f"По отменённому заказу №{order.id} всё же прошла оплата — вернули "
        f"{amount(refunded_amount)} ₽.\n"
        "Срок зачисления возврата зависит от вашего банка. Если заказ всё-таки "
        "нужен — напишите, оформлю заново."
    )


def canceled_paid_stuck(order) -> str:
    return (
        f"По отменённому заказу №{order.id} всё же прошла оплата.\n"
        "Разбираемся с возвратом — менеджер свяжется с вами "
        f"{worktime.working_day_phrase()}."
    )


def refunded(order, refund_amount) -> str:
    # Чек возврата приходит в обоих случаях: при полном возврате ЮKassa
    # собирает его сама по чеку платежа, при частичном мы передаём `receipt`
    # (`yookassa_client.create_refund(full=False)`).
    return (
        f"Оформили возврат {amount(refund_amount)} ₽ по заказу №{order.id}.\n"
        "Деньги вернутся тем же способом, которым вы платили; срок зачисления "
        "зависит от банка.\n"
        "Чек возврата придёт на почту, которую вы указывали при заказе."
    )


def receipt_delayed(order, *, email: str = "", phone: str = "") -> str:
    return (
        f"Чек по заказу №{order.id} ещё не сформировался — задержка на стороне "
        f"кассы. С оплатой всё в порядке, чек придёт на "
        f"{receipt_destination(email, phone)}."
    )


def reminder_1(order, link: str) -> str:
    """Мягкое напоминание про выставленный счёт."""
    return (
        f"Заказ №{order.id} на {amount(order.total)} ₽ ждёт оплаты 🙂\n"
        f"Оплатить: {link}\n"
        "Если с оплатой что-то не получается или хотите поменять заказ — просто "
        "напишите."
    )


def reminder_2(order, link: str, expires_at) -> str:
    """Последнее напоминание: скоро срок счёта истечёт.

    «Счёт действителен до», а не «ссылка перестанет работать»: закрыть
    ссылку у ЮKassa мы не можем, она закрывается сама и не по нашим часам.
    """
    return (
        f"Счёт по заказу №{order.id} действителен до {worktime.hhmm(expires_at)} "
        "по Москве.\n"
        f"Оплатить: {link}\n"
        "Если не успеете — ничего страшного, напишите, и я пришлю новую ссылку."
    )


def handed_over(order, *, carrier: str, number: str = "", tracking_url: str = "") -> str:
    """Посылку принял перевозчик — не «зарегистрировали», а физически забрал.

    `carrier` — «СДЭК» или «Ozon»: «передана в СДЭК». У Ozon публичной
    страницы отслеживания нет, за отправлением следят в приложении.
    """
    lines = [f"Посылка по заказу №{order.id} передана в {carrier} и едет к вам 🚚"]
    if number:
        lines.append(f"Трек-номер: {number}")
    if tracking_url:
        lines.append(f"Отследить: {tracking_url}")
    elif number:
        lines.append("Отследить: в приложении Ozon, в разделе заказов")
    return "\n".join(lines)


def delivered(order, *, receipt_email: str = "") -> str:
    """Посылка вручена.

    Про закрывающий чек предупреждаем заранее: второе письмо из ЮKassa по
    уже оплаченному заказу иначе выглядит как повторное списание.
    """
    lines = [f"Заказ №{order.id} вручён — спасибо, что выбрали нас! 🍵"]
    if receipt_email:
        lines.append(
            f"На {receipt_email} придёт итоговый чек о получении товара. Это не "
            "новое списание, а закрывающий документ к уже оплаченному заказу."
        )
    lines.append("Будет здорово, если напишете, как вам чай.")
    return "\n".join(lines)


def at_pickup_point(order, *, carrier: str, address: str = "", storage_until=None,
                    postamat: bool = False) -> str:
    """3.16 / 3.17. Посылка ждёт в пункте выдачи.

    «Хранится до» — только если перевозчик отдал дату: придуманный срок
    хуже никакого. Код получения у Ozon — в приложении, у СДЭКа — в SMS.
    """
    place = f"в постамате {carrier}" if postamat else f"в пункте выдачи {carrier}"
    head = f"Посылка по заказу №{order.id} ждёт вас {place}" + (f": {address}." if address else ".")
    storage = f"Хранится до {day_month(storage_until)}." if storage_until else ""
    if carrier == "Ozon":
        lines = [head, "Код для получения — в приложении Ozon, в разделе заказов.", storage]
    else:
        lines = [
            head, storage,
            f"Как получить, {carrier} сообщит в SMS. Если что-то пойдёт не так — пишите сюда.",
        ]
    return "\n".join(line for line in lines if line)


def pickup_expiring(order, storage_until) -> str:
    """3.18. Срок хранения заканчивается — за день до него."""
    return (
        f"Посылка по заказу №{order.id} ждёт в пункте выдачи до {day_month(storage_until)}. "
        "После этого её вернут нам — заберите, пожалуйста, до этой даты 🙏\n"
        "Если не успеваете — напишите, подскажем, что можно сделать."
    )


def not_delivered(order) -> str:
    """Посылку не вручили, она едет обратно к нам."""
    return (
        f"Посылка по заказу №{order.id} не была получена и возвращается к нам.\n"
        "Менеджер свяжется с вами "
        f"{worktime.working_day_phrase()}: вернём деньги или отправим заново — "
        "как вам удобнее."
    )


_NUDGE_CLOSING = "Передумали — просто не отвечайте, больше напоминать не буду 🙂"


def draft_nudge_priced(
    items, *, delivery_label: str, total, threshold_gap=None, approximate: bool = False
) -> str:
    """3.20. Клиент замолчал после названной цены.

    Строка про порог — только если до бесплатной доставки не хватает не
    больше одной пачки: «добавьте ещё 2000 ₽» уже не подсказка, а давление.
    Это решает вызывающий, передавая `threshold_gap`. `approximate` — цена
    Ozon до выбора пункта: она предварительная, и точной её не называем.
    """
    lines = [
        f"Заказ ждёт вас: {composition(items)} и доставка {delivery_label} — "
        f"итого {'около ' if approximate else ''}{amount(total)} ₽."
    ]
    if threshold_gap:
        lines.append(
            f"До бесплатной доставки не хватает {amount(threshold_gap)} ₽ — "
            "можно добавить ещё пачку."
        )
    lines.append(f"Оформить? Если нужно что-то поменять — напишите, поправлю. {_NUDGE_CLOSING}")
    return "\n".join(lines)


def draft_nudge_unpriced(items, *, items_total) -> str:
    """3.21. Клиент выбрал чай и замолчал до расчёта доставки."""
    return (
        f"Вы выбирали {composition(items)} — {amount(items_total)} ₽. Посчитать "
        "доставку? Назовите город — скажу цену и ближайшие пункты выдачи.\n"
        f"{_NUDGE_CLOSING}"
    )


def escalation_waiting() -> str:
    return "Вопрос у менеджера, он ответит здесь же, как только освободится 🙏"


# --- менеджеру -------------------------------------------------------------


def manager_unpaid(order, payment_status: str, minutes: int) -> str:
    return (
        f"⚠️ Заказ №{order.id}: оплата так и не пришла\n"
        f"Прошло больше {minutes} минут — срок ссылки ЮKassa, платёж в статусе "
        f"«{payment_status}». Клиенту написали, что срок истёк: он может оформить "
        "заново одним «да». Писать ему самому не нужно."
    )


def manager_receipt_stuck(order, receipt_status: str) -> str:
    # Единственное уведомление с прямым риском штрафа: по 54-ФЗ чек нужно
    # отправить покупателю не позднее следующего рабочего дня после оплаты.
    return (
        f"🚨 Заказ №{order.id}: чек не зарегистрирован\n"
        f"Статус чека — «{receipt_status or 'неизвестен'}». По 54-ФЗ чек нужно "
        "отправить покупателю не позднее следующего рабочего дня после оплаты — "
        "срок уже на пределе. Сегодня же напишите в поддержку ЮKassa."
    )


def manager_client_unreachable(order, event_type: str, error: str) -> str:
    """ВК не принял сообщение клиенту — например, тот запретил писать."""
    return (
        f"⚠️ Заказ №{order.id}: сообщение клиенту не доставлено\n"
        f"Событие «{event_type}». ВК ответил: {error}\n"
        "Скажите клиенту сами — бот повторять не будет."
    )


def manager_question(question: str, reason: str, link: str) -> str:
    """Вопрос клиента передан менеджеру. `question` и `reason` уже экранированы."""
    return (
        f"❓ <b>Вопрос клиента</b>\n{question}\n\n"
        f"<b>Почему передано</b>\n{reason}\n\n{link}"
    )


def manager_carrier_failed(order_id, carrier: str, link: str) -> str:
    """Оплаченный заказ не завёлся у перевозчика."""
    number = f"№{order_id} " if order_id else ""
    return (
        f"⚠️ Заказ {number}оплачен, но в {carrier} не уехал.\n"
        "Деньги получены, клиенту написали, что менеджер свяжется. Заведите "
        "отправление руками и пришлите клиенту трек.\n"
        f"Диалог: {link}"
    )


def manager_not_handed_over(order, paid_at, carrier: str, link: str) -> str:
    """Оплачен, а перевозчику не сдан дольше обещанного."""
    return (
        f"⏰ Заказ №{order.id} оплачен {worktime.to_msk(paid_at):%d.%m в %H:%M}, "
        f"но ещё не сдан в {carrier}.\n"
        f"Клиенту обещали сдать {settings.handover_promise}. Проверьте сборку.\n"
        f"{link}"
    )


def manager_escalation_reping(question: str, reason: str, waited_minutes: int, link: str) -> str:
    return (
        f"⏰ Вопрос клиента ждёт ответа {waited_minutes // 60} ч рабочего времени\n"
        f"{question}\n\nПочему передано: {reason}\n\n{link}"
    )


def manager_double_payment(order, payment, refund) -> str:
    return (
        f"↩️ <b>Заказ №{order.id}: повторная оплата возвращена</b>\n"
        f"Платёж {payment.id} на {amount(payment.amount)} ₽ — возврат "
        f"{refund.id}, статус «{refund.status}».\n"
        "Заказ оплачен один раз, отправление в работе. Клиенту сказали."
    )


def manager_double_payment_stuck(order, payment, error: str) -> str:
    return (
        f"🚨 <b>Заказ №{order.id}: повторная оплата НЕ возвращена</b>\n"
        f"Платёж {payment.id} на {amount(payment.amount)} ₽.\n"
        f"ЮKassa отказала: {error}\n"
        "Верните вручную в кабинете ЮKassa — деньги клиента у нас, клиенту "
        "обещали, что менеджер свяжется."
    )


def manager_order_canceled(order) -> str:
    return (
        f"❌ <b>Заказ №{order.id} отменён клиентом</b>\n"
        f"На {amount(order.total)} ₽, не оплачен, отправление не заводили. "
        "Счёт закрыт; если клиент всё же заплатит по старой ссылке, деньги "
        "вернутся автоматически."
    )


def manager_canceled_paid(order, payment, refund) -> str:
    return (
        f"↩️ <b>Заказ №{order.id}: оплата отменённого заказа возвращена</b>\n"
        f"Платёж {payment.id} на {amount(payment.amount)} ₽ — возврат "
        f"{refund.id}, статус «{refund.status}».\n"
        "Клиент отменил заказ до оплаты, отправление не заводили. Клиенту сказали."
    )


def manager_canceled_paid_stuck(order, payment, error: str) -> str:
    return (
        f"🚨 <b>Заказ №{order.id}: оплата отменённого заказа НЕ возвращена</b>\n"
        f"Платёж {payment.id} на {amount(payment.amount)} ₽.\n"
        f"ЮKassa отказала: {error}\n"
        "Вернуть вручную в кабинете ЮKassa — деньги клиента у нас, "
        "отправление не заводили."
    )


def manager_not_delivered(order, carrier_status: str) -> str:
    return (
        f"↩️ <b>Заказ №{order.id} не вручён — посылка возвращается</b>\n"
        f"Статус перевозчика: {carrier_status}.\n"
        "Закрывающий чек по такому заказу не формируется. Когда посылка "
        "вернётся — полный возврат в кабинете ЮKassa: чек возврата ЮKassa "
        "соберёт сама по чеку оплаты. Коды маркировки освободятся сами, "
        "когда придёт уведомление о возврате. Клиенту сказали."
    )


def manager_carrier_trouble(order, carrier_status: str) -> str:
    return (
        f"⚠️ <b>Заказ №{order.id}: заминка у перевозчика</b>\n"
        f"Статус: {carrier_status}. Проверьте отправление в кабинете."
    )


def manager_settlement_problem(order, reason: str, *, urgent: bool = True) -> str:
    """Закрывающий чек не ушёл или застрял — что и как исправить."""
    return (
        f"{'🚨' if urgent else '⚠️'} <b>Заказ №{order.id}: {reason}</b>\n"
        "Закрывающий чек (зачёт предоплаты с кодами маркировки) не сформирован.\n"
        f"Исправить и отправить: ссылка на сборку — scripts/api.sh orders/{order.id}/pack-link, "
        f"затем scripts/api.sh orders/{order.id}/settlement-receipt"
    )


def pack_card_line(url: str, expires) -> str:
    """Строка со ссылкой на сборку в карточке оплаченного заказа."""
    return (
        f'📦 <a href="{url}">Собрать заказ — сканировать коды</a> '
        f"(ссылка до {worktime.to_msk(expires):%d.%m %H:%M} МСК)"
    )


def manager_pack_link(order_id, url: str, expires) -> str:
    """Свежая ссылка на сборку по команде orders/<N>/pack-link."""
    return (
        f'📦 <a href="{url}">Собрать заказ №{order_id} — сканировать коды</a>\n'
        f"Ссылка действует до {worktime.to_msk(expires):%d.%m %H:%M} МСК."
    )


def manager_refund_codes_released(count: int) -> str:
    return f"Коды маркировки освобождены: {count} шт., снова в наличии."


def manager_refund_after_settlement() -> str:
    return (
        "⚠️ Возврат после закрывающего чека: чек возврата должен быть с "
        "полным расчётом и кодами маркировки возвращённых пачек. Проверьте "
        "чек возврата в кабинете ЮKassa; при расхождении — их поддержка."
    )
