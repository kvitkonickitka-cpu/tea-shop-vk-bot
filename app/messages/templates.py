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

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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
REPEAT_NUDGE = "repeat_nudge"
# Повторные касания после вручения (кроме «Повторить» — оно выше).
FEEDBACK_ASK = "feedback_ask"
SECOND_TOUCH = "second_touch"
REACTIVATION = "reactivation"


def _amount_value(value):
    """Число из того, что пришло: число, строка, словарь или объект суммы.

    ЮKassa отдаёт сумму как {"value": "917.00", "currency": "RUB"}, а её
    SDK — объектом с полем value. Раньше такой объект, попав в шаблон,
    печатался целиком: «namespace(value='917.00') ₽».
    """
    if isinstance(value, dict):
        value = value.get("value")
    elif not isinstance(value, (int, float, str, Decimal)) and hasattr(value, "value"):
        value = value.value
    return Decimal(str(value).strip().replace(" ", "").replace(",", "."))


def amount(value) -> str:
    """Сумма для текста: «917», с копейками — «917,50».

    Без экспоненты: `f"{x:g}"` превращал 1 500 000 в «1.5e+06».
    """
    try:
        number = _amount_value(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    if number == number.to_integral_value():
        return f"{number:.0f}"
    return f"{number:.2f}".replace(".", ",")


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


def paid(order, *, email: str = "", phone: str = "", posting: str = "", cdek: bool = False,
         expected: str = "") -> str:
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
    if expected and (posting or cdek):
        # Дата считается от момента оплаты, а не от сводки: прошёл час — она
        # могла сдвинуться за вечернюю отсечку.
        lines.append(f"Ждите {expected}.")
    return "\n".join(lines)


def delivery_place(method: str | None, label: str | None) -> str:
    """Куда везём — для текста клиенту: «пункт выдачи Ozon, <адрес>».

    Метка черновика («Ozon, пункт выдачи: …») внутренняя и читается плохо.
    """
    label = label or ""
    address = label.split(": ", 1)[1] if ": " in label else ""
    place = {
        "ozon_pvz": "пункт выдачи Ozon",
        "cdek_pvz": "пункт выдачи СДЭК",
        "cdek_courier": "курьер СДЭК",
    }.get(method or "")
    if place is None:
        return label or "—"
    return f"{place}, {address}" if address else place


def invoice_summary(
    *,
    order_id,
    items: list[dict],
    delivery_method: str | None,
    delivery_label: str | None,
    delivery_cost,
    name: str,
    phone: str,
    email: str,
    total,
    link: str,
    eta: str = "",
    button: bool = False,
    surcharge: bool = False,
) -> str:
    """2.1. Сводка и ссылка одним сообщением — вместо «Оформляем?» и «да».

    `button` — клиент видит кнопку «Оплатить»: тогда ссылки в тексте нет
    (см. `keyboard.shows_link_button`).

    Подтверждением стала сама оплата, поэтому всё, что клиент мог бы
    проверить на «Оформляем?», стоит здесь: состав, пункт, получатель и
    почта полностью — опечатку в ней надо увидеть до оплаты, чек уйдёт туда.
    """
    head = f"Заказ №{order_id} — проверьте, всё ли верно:" if order_id else "Проверьте, всё ли верно:"
    lines = [head, *_order_block(items, delivery_method, delivery_label, delivery_cost, eta, surcharge)]
    lines += _payment_block(name=name, phone=phone, email=email, total=total, link=link, button=button)
    return "\n".join(lines)


def _delivery_cost_text(delivery_cost, surcharge: bool = False) -> str:
    if not delivery_cost:
        return "бесплатно"
    # Доплата за перевозчика быстрее бесплатного — так и называем: иначе
    # клиент выше порога читает «245 ₽» как «порог не сработал».
    return f"доплата {amount(delivery_cost)} ₽" if surcharge else f"{amount(delivery_cost)} ₽"


def _order_block(items, delivery_method, delivery_label, delivery_cost, eta, surcharge=False) -> list[str]:
    """Состав, доставка и срок — первый абзац сводки со ссылкой."""
    lines = []
    for item in items:
        quantity = int(item.get("quantity") or 1)
        lines.append(
            f"• {item.get('name', 'товар')} × {quantity} — "
            f"{amount(float(item.get('price') or 0) * quantity)} ₽"
        )
    cost = _delivery_cost_text(delivery_cost, surcharge)
    lines.append(f"Доставка: {delivery_place(delivery_method, delivery_label)} — {cost}")
    if eta:
        lines.append(f"Срок: {eta}")
    return lines


def _payment_block(*, name, phone, email, total, link, button, extra: str = "") -> list[str]:
    """Получатель, итог и оплата — абзацами, как их читают с телефона.

    Почта здесь одна — в строке получателя: два раза один и тот же адрес в
    сводке только удлиняли её. Кнопка оплаты — после строки «если что-то не
    так»: так она стоит прямо над самой кнопкой.
    """
    lines = [
        "",
        f"Получатель: {name}, {phone}, {email}",
        f"Итого к оплате с учётом доставки: {amount(total)} ₽",
        "",
        f"{_link_noun(button)} действует {settings.payment_invoice_ttl_minutes} минут. После оплаты "
        "пришлём чек на указанную в заказе почту и сразу передадим заказ в доставку.",
    ]
    if extra:
        lines += ["", extra]
    lines += ["", "Если что-то не так — напишите, поправлю и пришлю новую ссылку.", "", pay_line(link, button)]
    if settings.conditions_url:
        lines += ["", f"Условия покупки, доставки и возврата: {settings.conditions_url}"]
    return lines


def manager_paid_old_variant(order, link: str) -> str:
    """Клиент поправил заказ после ссылки, а заплатил по старой."""
    return (
        f"⚠️ <b>Заказ №{order.id}: оплачен прошлый вариант заказа</b>\n"
        "Клиент поправил заказ после ссылки, но заплатил по старой. Отправляем "
        f"то, что оплачено: {composition(order.items or [])}, "
        f"{delivery_place(order.delivery_method, (order.details or {}).get('delivery_label'))}, "
        f"итого {amount(order.total)} ₽. Уточните у клиента, нужен ли ему новый вариант.\n"
        f"{link}"
    )


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


def pay_line(link: str, button: bool = False) -> str:
    """Строка оплаты: ссылка текстом — только когда кнопки клиент не увидит."""
    return "Оплатить — кнопкой ниже 👇" if button else f"Оплатить: {link}"


def _link_noun(button: bool) -> str:
    return "Кнопка оплаты" if button else "Ссылка"


def reminder_1(order, link: str, button: bool = False) -> str:
    """Мягкое напоминание про выставленный счёт."""
    return (
        f"Заказ №{order.id} на {amount(order.total)} ₽ ждёт оплаты 🙂\n"
        f"{pay_line(link, button)}\n"
        "Если с оплатой что-то не получается или хотите поменять заказ — просто "
        "напишите."
    )


def reminder_2(order, link: str, expires_at, button: bool = False) -> str:
    """Последнее напоминание: скоро срок счёта истечёт.

    «Счёт действителен до», а не «ссылка перестанет работать»: закрыть
    ссылку у ЮKassa мы не можем, она закрывается сама и не по нашим часам.
    """
    return (
        f"Счёт по заказу №{order.id} действителен до {worktime.hhmm(expires_at)} "
        "по Москве.\n"
        f"{pay_line(link, button)}\n"
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


def delivered(
    order, *, receipt_email: str = "", brewing: list[dict] | None = None,
    guide_url: str = "", ask_feedback: bool = True,
) -> str:
    """Посылка вручена.

    Про закрывающий чек предупреждаем заранее: второе письмо из ЮKassa по
    уже оплаченному заказу иначе выглядит как повторное списание.

    `brewing` — как заваривать купленное (до двух товаров: название, текст,
    видео). Блок заварки встаёт на место «напишите, как вам чай»: об этом
    через несколько дней спросит оценка кнопками. Без заварки строка
    остаётся, если отдельной оценки нет (`ask_feedback`).
    """
    lines = [f"Заказ №{order.id} вручён — спасибо, что выбрали нас! 🍵"]
    if receipt_email:
        lines.append(
            f"На {receipt_email} придёт итоговый чек о получении товара. Это не "
            "новое списание, а закрывающий документ к уже оплаченному заказу."
        )
    if brewing:
        lines.append("")
        lines.append("Как заваривать:")
        for block in brewing:
            lines.append(f"• {block['name']}: {block['text']}")
            if block.get("video"):
                lines.append(f"  Видео: {block['video']}")
        if guide_url:
            lines.append(f"Все способы заварки: {guide_url}")
    elif ask_feedback:
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


def repeat_nudge(items, *, weeks: int) -> str:
    """3.22. Чай, наверное, подходит к концу."""
    unit = "недели" if weeks == 1 else "недель"
    return (
        f"Здравствуйте! Около {weeks} {unit} назад вы получили {composition(items)} — "
        "чай, наверное, подходит к концу 🍵\n"
        "Повторить заказ с доставкой туда же? Или подскажу, что попробовать ещё.\n"
        "Если не хотите таких напоминаний — напишите «стоп»."
    )


STOP_LINE = "Если не хотите таких сообщений — напишите «стоп»."


def _offer(offer: str, price, description: str) -> str:
    return f"{offer} ({amount(price)} ₽)" + (f" — {description}" if description else "")


def second_touch(*, offer: str, price, description: str = "", source: str = "", rating=None) -> str:
    """Второй шанс: другой сорт. Не «повторить», а «попробовать»."""
    if rating == "great" and source:
        head = f"Здравствуйте! Вам понравился {source} — попробуйте {_offer(offer, price, description)}."
    elif rating == "no":
        head = f"Здравствуйте! Подобрала вам другой чай — {_offer(offer, price, description)}."
    elif source:
        head = f"Здравствуйте! К {source} у нас советуют {_offer(offer, price, description)}."
    else:
        head = f"Здравствуйте! Хотите попробовать {_offer(offer, price, description)}?"
    return f"{head}\nЕсли захотите — нажмите «Взять» или просто напишите.\n{STOP_LINE}"


def reactivation(*, offers: list[dict], source: str = "", novelties: bool = False) -> str:
    """Реактивация: коротко, без давления и скидок. `offers` — название, цена, описание."""
    if novelties:
        head = "Здравствуйте! Давно не виделись 🍵 У нас появилось новое:"
    elif source:
        head = f"Здравствуйте! Давно не виделись 🍵 К {source} у нас советуют:"
    else:
        head = "Здравствуйте! Давно не виделись 🍵 Возможно, вам понравится:"
    lines = [head]
    lines += [f"• {_offer(o['name'], o['price'], o.get('description', ''))}" for o in offers]
    lines.append("Если захотите — нажмите «Взять», повторите прошлый заказ или попросите подобрать чай.")
    lines.append(STOP_LINE)
    return "\n".join(lines)


def marketing_stopped() -> str:
    """3.23. Ответ на «стоп»."""
    return (
        "Хорошо, больше не буду присылать напоминания. Сообщения по вашим "
        "заказам и доставке будут приходить как обычно."
    )


# Подсказки рядом с кнопками: не у всех приложений кнопки есть, и ответить
# словами должно быть так же просто.
POINTS_HINT = "Можно нажать кнопку или написать номер пункта."
EMAIL_HINT = "Или напишите почту заново."
ASK_RECIPIENT = "Пришлите одним сообщением ФИО, телефон и почту — сразу пришлю счёт."


def button_stale() -> str:
    """Нажата старая кнопка: заказ с тех пор изменился."""
    return "Эта кнопка уже неактуальна."


def delivery_options(*, base_name: str, base_price, base_when: str, fast_name: str = "", fast_price=0,
                     fast_when: str = "", free: bool = False) -> str:
    """Самая дешёвая доставка и, если есть, быстрая — одной фразой.

    «Ozon — 117 ₽, получите ≈ 10 октября. Нужно быстрее — СДЭК 245 ₽, получите ≈ 8 октября»
    Выше порога дешёвая бесплатна, а за быструю — доплата.
    """
    when = f", {base_when}" if base_when else ""
    if free:
        line = f"Доставка {base_name} — бесплатно{when}"
    else:
        line = f"{base_name} — {amount(base_price)} ₽{when}"
    if fast_name:
        fast = f", {fast_when}" if fast_when else ""
        price = f"с доплатой {amount(fast_price)} ₽" if free else f"{amount(fast_price)} ₽"
        line += f". Нужно быстрее — {fast_name} {price}{fast}"
    return line


def point_chosen(
    *, address: str, delivery_cost, total, ask_recipient: bool, eta: str = "", ask: str = "",
    surcharge: bool = False,
) -> str:
    """Пункт выбран кнопкой, а данных получателя ещё нет. `ask` — своя просьба вместо общей."""
    cost = _delivery_cost_text(delivery_cost, surcharge)
    lines = [f"Записала пункт: {address}. Доставка — {cost}, итого {amount(total)} ₽."]
    if eta:
        lines.append(f"Срок: {eta}.")
    if ask_recipient:
        lines.append(ask or ASK_RECIPIENT)
    return "\n".join(lines)


# Ссылка на карту — сразу при вопросе «куда»: клиенту проще выбрать пункт
# или постамат глазами и прислать его адрес, чем вспоминать улицу.
OZON_POINTS_MAP = "https://www.ozon.ru/geo/"
_POINT_MAPS = {"Ozon": OZON_POINTS_MAP, "СДЭК": "https://www.cdek.ru/ru/offices"}


def ask_point_address(carrier: str = "Ozon") -> str:
    """Город большой, улица не названа: просим адрес пункта вместо списка."""
    return (
        f"Пунктов выдачи {carrier} в городе много — подскажите, какой удобен: напишите "
        "улицу и номер дома пункта, скопируйте его адрес с карты или пришлите скриншот. "
        f"Все пункты {carrier} на карте: {_POINT_MAPS[carrier]}"
    )


STOREFRONT_ORDER = "storefront_order"


def storefront_points(
    *,
    order_id,
    items: list[dict],
    items_total,
    shown: list[dict],
    per_point_prices: bool,
    delivery_cost,
    ask: str,
    hint: str = "",
) -> str:
    """Заказ из витрины: сразу пункты рядом с адресом из заказа и просьба о данных."""
    listed = ", ".join(f"{item['name']} × {item['quantity']}" for item in items)
    lines = [f"Заказ №{order_id} принят: {listed} — {amount(items_total)} ₽."]
    head = "Ближайшие пункты выдачи Ozon — дешевле всего, заберёте сами"
    if not per_point_prices and delivery_cost is not None:
        head += f", доставка около {amount(delivery_cost)} ₽"
    lines.append(head + ":")
    for point in shown:
        price = f" — {amount(point['price'])} ₽" if per_point_prices and point.get("price") is not None else ""
        lines.append(f"{point['n']}) {point['address']}{price}")
    lines.append("Быстрее, но дороже — пункт выдачи СДЭК или курьер: напишите, если нужен он.")
    lines.append(ask)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def storefront_ask_point(*, order_id, items: list[dict], items_total, city: str, delivery_cost, ask: str) -> str:
    """Заказ из витрины, город большой, а улицы в адресе нет — просим адрес пункта."""
    listed = ", ".join(f"{item['name']} × {item['quantity']}" for item in items)
    if delivery_cost is None:
        cost = ""
    else:
        cost = f" — около {amount(delivery_cost)} ₽" if delivery_cost else " — бесплатно"
    return "\n".join([
        f"Заказ №{order_id} принят: {listed} — {amount(items_total)} ₽.",
        f"Дешевле всего — пункт выдачи Ozon в городе {city}{cost}, заберёте сами. "
        "Быстрее, но дороже — пункт выдачи СДЭК или курьер: напишите, если нужен он.",
        ask_point_address("Ozon"),
        ask,
    ])


def storefront_carriers(*, order_id, items: list[dict], items_total, city: str, options: list[dict],
                        button: bool) -> str:
    """Заказ из «Товаров»: два перевозчика до города клиента — выбор кнопкой.

    Отвечает и на шаблонный вопрос ВК «как оплатить заказ и когда сможете
    доставить?»: сроки — здесь, оплата — ссылкой после выбора доставки.
    """
    listed = ", ".join(f"{item['name']} × {item['quantity']}" for item in items)
    lines = [f"Заказ №{order_id} принят: {listed} — {amount(items_total)} ₽.", "", f"Доставка в {city}:"]
    for option in options:
        cost = "бесплатно" if not option["client_cost"] else f"{amount(option['client_cost'])} ₽"
        when = f", {option['eta_phrase']}" if option.get("eta_phrase") else ""
        lines.append(f"• {option['carrier']}, пункт выдачи — {cost}{when}")
    lines += [
        "",
        "Оплата — онлайн по ссылке: выберите доставку и пункт, пришлите почту для чека, "
        "и я сразу пришлю ссылку на оплату.",
        "Выберите доставку кнопкой ниже 👇" if button else "Напишите, какую доставку выбираете: Ozon или СДЭК.",
    ]
    return "\n".join(lines)


def carrier_button(option: dict) -> str:
    cost = "бесплатно" if not option["client_cost"] else f"{amount(option['client_cost'])} ₽"
    return f"{option['carrier']} — {cost}"


def storefront_carrier_chosen(*, carrier: str, city: str, delivery_cost, eta: str, shown: list[dict],
                              asked: bool, recipient: dict | None, hint: str = "",
                              last: tuple[str, str, str] | None = None, button: bool = True) -> str:
    """Перевозчик выбран: пункт и почта для чека — и сразу счёт."""
    cost = "бесплатно" if not delivery_cost else f"{amount(delivery_cost)} ₽"
    lines = [f"{carrier}, пункт выдачи — {cost}" + (f", {eta}." if eta else ".")]
    if asked:
        lines += ["", ask_point_address(carrier)]
    else:
        lines += ["", f"Пункты выдачи {carrier} в городе {city}:"]
        lines += [f"{point['n']}) {point['address']}" for point in shown]
    lines.append("")
    if last:
        lines.append(ask_last_recipient(*last, button=button))
    elif recipient:
        lines.append(
            f"Получатель из заказа: {recipient['name']}, {recipient['phone']}. Пришлите, пожалуйста, "
            "почту — на неё придёт чек об оплате. Как только будут пункт и почта, сразу пришлю ссылку на оплату."
        )
    else:
        lines.append(
            "Пришлите ФИО и телефон получателя и почту — она нужна, чтобы отправить чек об оплате. "
            "Как только будут пункт и данные, сразу пришлю ссылку на оплату."
        )
    if hint:
        lines.append(hint)
    return "\n".join(lines)


STOREFRONT_WITH_POINT_ALL = "Вместе с пунктом пришлите ФИО, телефон и почту — сразу пришлю счёт."


def storefront_with_point_email(name: str, phone: str) -> str:
    """Получатель есть в заказе витрины, пункт ещё не назван."""
    return (
        f"Получатель из заказа: {name}, {phone}. Вместе с пунктом пришлите почту для чека — "
        "сразу пришлю счёт. Если получатель другой — напишите ФИО, телефон и почту."
    )


STOREFRONT_ASK_ALL = "Выберите пункт и одним сообщением пришлите ФИО, телефон и почту — сразу пришлю счёт."


def storefront_ask_email(name: str, phone: str) -> str:
    """Получатель есть в заказе витрины — показать для проверки и попросить почту."""
    return (
        f"Получатель из заказа: {name}, {phone}. Выберите пункт и пришлите почту для чека — "
        "сразу пришлю счёт. Если получатель другой — напишите ФИО, телефон и почту."
    )


def storefront_ask_email_only(name: str, phone: str) -> str:
    """Пункт выбран, получатель из заказа витрины — осталась почта."""
    return f"Пришлите почту для чека — получатель {name}, {phone}, сразу пришлю счёт."


def ask_last_recipient(name: str, phone: str, email: str, *, button: bool = True) -> str:
    """Прошлый получатель постоянного клиента — кнопкой или словом «да»."""
    how = "Нажмите «Да, на эти данные»" if button else "Ответьте «да»"
    return (
        f"Получатель как в прошлый раз — {name}, {phone}, {email}? "
        f"{how} или пришлите ФИО, телефон и почту — сразу пришлю счёт."
    )


def storefront_ask_last(name: str, phone: str, email: str, button: bool = True) -> str:
    """Заказ витрины от постоянного клиента: пункт и прошлый получатель."""
    return "Выберите пункт. " + ask_last_recipient(name, phone, email, button=button)


def taken(*, name: str, price, upsell: str = "", upsell_price=None, gap=None) -> str:
    """«Взять <сорт>» под консультацией: то, что модель писала после «беру»."""
    lines = [f"Записала: {name} — {amount(price)} ₽."]
    extra = upsell_line(upsell, upsell_price, gap)
    if extra:
        lines.append(extra)
    lines.append("Если нужно больше пачек — напишите сколько.")
    lines.append(ASK_WHERE)
    return "\n".join(lines)


def item_added(*, name: str, items_total, gap=None, free: bool = False, next_step: str = "") -> str:
    """Допродажа кнопкой «Добавить»."""
    lines = [f"Добавила {name}. Товаров на {amount(items_total)} ₽."]
    if free:
        lines.append("Доставка для вас будет бесплатной 🙂")
    elif gap:
        lines.append(f"До бесплатной доставки не хватает {amount(gap)} ₽.")
    if next_step:
        lines.append(next_step)
    return "\n".join(lines)


ASK_WHERE = (
    "Куда везти — город и улица, где удобно забрать? "
    f"Пункты выдачи и постаматы Ozon на карте: {OZON_POINTS_MAP} — можно выбрать там и прислать адрес."
)
ASK_POINT_AND_RECIPIENT = (
    "Выберите пункт выдачи и одним сообщением пришлите ФИО, телефон и почту — сразу пришлю счёт."
)
ASK_POINT = "Выберите пункт выдачи — сразу пришлю счёт."


def recipient_written(*, name: str, phone: str, email: str, next_step: str = "") -> str:
    """Прошлый получатель записан кнопкой «Да, на эти данные»."""
    text = f"Записала получателя: {name}, {phone}, {email}."
    return f"{text}\n{next_step}" if next_step else text


def email_written(email: str, next_step: str = "") -> str:
    """Почта с исправленной опечаткой записана кнопкой «Да, …»."""
    text = f"Записала почту {email}."
    return f"{text}\n{next_step}" if next_step else text


def returning_offer(
    *,
    items: list[dict],
    delivery_method: str,
    delivery_label: str,
    delivery_cost,
    name: str,
    phone: str,
    email: str,
    total,
    upsell: str = "",
    upsell_price=None,
    gap=None,
    eta: str = "",
) -> str:
    """Постоянному клиенту — весь заказ одним сообщением вместо трёх вопросов.

    Пункт и цена уже проверены, почта — тоже. Ничего не записано: ждём
    «Оформить» или «да».
    """
    lines = ["Оформить как в прошлый раз?"]
    for item in items:
        quantity = int(item.get("quantity") or 1)
        lines.append(
            f"• {item.get('name', 'товар')} × {quantity} — "
            f"{amount(float(item.get('price') or 0) * quantity)} ₽"
        )
    cost = _delivery_cost_text(delivery_cost)
    lines.append(f"Доставка: {delivery_place(delivery_method, delivery_label)} — {cost}")
    if eta:
        lines.append(f"Срок: {eta}")
    lines.append(f"Получатель: {name}, {phone}, {email}")
    lines.append(f"Итого: {amount(total)} ₽")
    if upsell:
        lines.append(upsell_line(upsell, upsell_price, gap))
    lines.append(
        "Нажмите «Оформить» или ответьте «оформить» — пришлю ссылку на оплату. "
        "Если что-то поменять — напишите."
    )
    return "\n".join(lines)


def upsell_line(upsell: str, upsell_price=None, gap=None) -> str:
    """«К нему можно добавить …» — строка допродажи в сводках, или пусто."""
    if not upsell:
        return ""
    extra = f"К нему можно добавить {upsell}"
    extra += f" — {amount(upsell_price)} ₽" if upsell_price is not None else ""
    if gap:
        extra += f", до бесплатной доставки как раз не хватает {amount(gap)} ₽"
    return extra + "."


def returning_invoice(
    *,
    items: list[dict],
    delivery_method: str | None,
    delivery_label: str | None,
    delivery_cost,
    name: str,
    phone: str,
    email: str,
    total,
    link: str,
    eta: str = "",
    upsell: str = "",
    upsell_price=None,
    gap=None,
    button: bool = False,
) -> str:
    """Постоянному клиенту — заказ как в прошлый раз сразу со ссылкой.

    Подтверждением служит оплата, поэтому вопроса «Оформить?» нет: всё, что
    клиент проверил бы перед ним, стоит здесь, и поправить можно словами.
    """
    lines = ["Как в прошлый раз — проверьте, всё ли верно:",
             *_order_block(items, delivery_method, delivery_label, delivery_cost, eta)]
    lines += _payment_block(name=name, phone=phone, email=email, total=total, link=link, button=button,
                            extra=upsell_line(upsell, upsell_price, gap))
    return "\n".join(lines)


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


_CONSENT = {"yes": "можно", "no": "нельзя", "unknown": "не спрашивали"}


RATINGS = {"great": "Очень понравился", "ok": "Нормально", "no": "Не моё"}


def feedback_ask(order, single_name: str = "") -> str:
    """Оценка через несколько дней после вручения. Кнопки — `RATINGS`."""
    about = single_name or f"чай из заказа №{order.id}"
    return f"Здравствуйте! Как вам {about}? 🍵"


def rated_great() -> str:
    return (
        "Спасибо, очень приятно! 🙏 Напишите пару слов о чае — с вашего "
        "разрешения опубликуем отзыв в сообществе."
    )


def rated_ok() -> str:
    return "Спасибо за честность! Что было бы лучше — крепче, мягче, другой вкус? Подберу."


def rated_no() -> str:
    return (
        "Жаль, что не подошёл 😔 Расскажите, что было не так — вкус, крепость, "
        "аромат? Подберу что-то ближе к вашему вкусу."
    )


def manager_rating(order_id, rating: str, link: str) -> str:
    """Оценка кнопкой: «Очень понравился» и «Не моё» — карточкой менеджеру."""
    mark = "⭐" if rating == "great" else "🤔"
    return (
        f"{mark} <b>Оценка заказа №{order_id}: «{RATINGS.get(rating, rating)}»</b>\n"
        + ("Бот спросил, что не подошло, и продолжает разговор — это не вопрос к вам, "
           "но загляните, если нужно.\n" if rating == "no" else "")
        + link
    )


def manager_feedback(order_id, text: str, consent: str, link: str) -> str:
    """4.14. Отзыв клиента. `text` уже экранирован."""
    return (
        f"⭐ <b>Отзыв по заказу №{order_id}</b>\n{text}\n\n"
        f"Публикация: {_CONSENT.get(consent, _CONSENT['unknown'])}\n{link}"
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
