"""Все шаблоны сообщений: клиенту в ВК и менеджеру в телеграм.

Одним файлом намеренно. Раньше тексты жили там, где отправлялись — в
уведомлении ЮKassa, в догляде за платежами, в сверке СДЭКа, — и вычитать
их целиком было невозможно: чтобы понять, что вообще получает клиент,
приходилось читать пять модулей.

Правила, по которым они написаны:

- клиент получает следствие и что делать дальше, а не диагностику. Код
  ошибки перевозчика, статус чека у ЮKassa и номер заявки — менеджеру;
- сумма без хвоста «.0»: 917 руб., а не 917.0;
- никаких обещаний, которых мы не контролируем: срок зачисления возврата
  зависит от банка, и так и написано.
"""

from __future__ import annotations

from app.core import worktime

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


def amount(value) -> str:
    """Сумма без лишнего нуля после точки."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


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
    lines = [
        "✅ Оплата получена, спасибо!",
        f"Заказ №{order.id} на {amount(order.total)} руб.",
        f"Чек придёт на {receipt_destination(email, phone)}.",
    ]
    if posting:
        # Клиенту важно не столько само отправление, сколько что делать
        # дальше: номер он увидит в приложении Ozon и там же будет следить
        # за доставкой, без нас и без менеджера.
        lines.append(
            f"Отправление Ozon: {posting} — по нему посылку видно в приложении "
            "и на сайте Ozon, там же отслеживается доставка."
        )
    elif cdek:
        lines.append("Передаём посылку в СДЭК. Трек-номер пришлём сюда, как только СДЭК его выдаст.")
    else:
        lines.append("Заказ передан в работу, менеджер свяжется с вами по отправке.")
    return "\n".join(lines)


def cdek_track(order, number: str, tracking_url: str) -> str:
    return (
        f"Посылка по заказу №{order.id} передана в СДЭК.\n"
        f"Трек-номер: {number}\nОтследить: {tracking_url}"
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
        f"Платёж по заказу №{order.id} на {amount(order.total)} руб. не прошёл.\n"
        "Можно попробовать ещё раз или другой картой — напишите сюда, пришлю "
        "новую ссылку 🙏"
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
        "Заказ оплачен один раз и уже в работе, ничего делать не нужно. "
        "Срок зачисления возврата зависит от вашего банка."
    )


def double_payment_stuck(order) -> str:
    return (
        f"По заказу №{order.id} пришла повторная оплата.\n"
        "Разбираемся с возвратом — менеджер свяжется с вами "
        f"{worktime.working_day_phrase()}. Заказ оплачен один раз и уже в работе."
    )


def refunded(order, refund_amount) -> str:
    return (
        f"Оформили возврат {amount(refund_amount)} ₽ по заказу №{order.id}.\n"
        "Сроки зачисления зависят от вашего банка."
    )


def receipt_delayed(order, *, email: str = "", phone: str = "") -> str:
    return (
        f"Чек по заказу №{order.id} пока не пришёл — задержка на стороне кассы.\n"
        f"Мы уже разбираемся, чек придёт на {receipt_destination(email, phone)}."
    )


def reminder_1(order, link: str) -> str:
    """Мягкое напоминание про выставленный счёт."""
    return (
        f"Напоминаю про заказ №{order.id} на {amount(order.total)} руб. — "
        "он ждёт оплаты 🙂\n"
        f"Оплатить: {link}\n"
        "Если что-то нужно поменять или передумали — просто напишите."
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


def escalation_waiting() -> str:
    return "Вопрос у менеджера, он ответит здесь же, как только освободится 🙏"


# --- менеджеру -------------------------------------------------------------


def manager_unpaid(order, payment_status: str, hours: int) -> str:
    return (
        f"⚠️ Заказ №{order.id}: оплата так и не пришла\n"
        f"Прошло больше {hours} ч, платёж в статусе «{payment_status}». "
        "Счёт закрыт, черновик возвращён клиенту — он может оформить заново."
    )


def manager_receipt_stuck(order, receipt_status: str, overdue: bool) -> str:
    return (
        f"⚠️ Заказ №{order.id}: чек не зарегистрирован\n"
        f"Статус чека «{receipt_status or 'неизвестен'}» "
        f"{'больше трёх суток' if overdue else 'отклонён'}. "
        "По документации ЮKassa — обращаться в их поддержку."
    )


def manager_client_unreachable(order, event_type: str, error: str) -> str:
    """ВК не принял сообщение клиенту — например, тот запретил писать."""
    return (
        f"⚠️ Заказ №{order.id}: сообщение клиенту не доставлено\n"
        f"Событие «{event_type}». ВК ответил: {error}\n"
        "Скажите клиенту сами — бот повторять не будет."
    )


def manager_escalation_reping(question: str, reason: str, waited_minutes: int, link: str) -> str:
    return (
        f"⏰ Вопрос клиента ждёт ответа {waited_minutes // 60} ч рабочего времени\n"
        f"{question}\n\nПочему передано: {reason}\n\n{link}"
    )


def manager_double_payment(order, payment, refund) -> str:
    return (
        f"↩️ <b>Заказ №{order.id}: повторная оплата возвращена</b>\n"
        f"Платёж {payment.id} на {amount(payment.amount)} руб — возврат "
        f"{refund.id}, статус «{refund.status}».\n"
        "Заказ оплачен один раз, отправление в работе. Клиенту сказали."
    )


def manager_double_payment_stuck(order, payment, error: str) -> str:
    return (
        f"🚨 <b>Заказ №{order.id}: повторная оплата НЕ возвращена</b>\n"
        f"Платёж {payment.id} на {amount(payment.amount)} руб.\n"
        f"ЮKassa отказала: {error}\n"
        "Вернуть вручную в кабинете ЮKassa — деньги клиента у нас."
    )
