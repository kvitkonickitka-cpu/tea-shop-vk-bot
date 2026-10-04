"""Срок доставки для клиента: сколько соберём и сдадим плюс сколько едет.

Перевозчик считает срок от сдачи посылки, а сдаём мы её не сразу — после
оплаты обещаем «в течение суток» (`handover_promise`, дни сборки —
`handover_days_min`–`handover_days`). Клиент, которому назвали «5 дней» по
Ozon, ждал бы 6. Поэтому срок называем целиком и раскладываем, из чего он
сложился:

    ≈ 6 дней: 1 день соберём и сдадим, 5 дней в пути у Ozon

Срок перевозчика приходит вместе с ценой — у СДЭКа в тарифе (рабочие дни),
у Ozon в расчёте (дни). Он хранится в черновике (`details.eta`) рядом с
ценой и обновляется при каждом её пересчёте: ответ инструмента в историю
диалога не попадает, а спросить «сколько ехать?» клиент может и через пару
сообщений. Нет срока у перевозчика — нет его и у нас: не выдумываем.

С `DELIVERY_DATE_ENABLED` срок называется датой — клиенту проще понять
«получите ≈ 10 октября», чем считать дни самому:

    ≈ 10 октября (1 день соберём, 5 дней в пути у Ozon)

Дата считается в момент показа: сегодня по Москве (после `ship_cutoff_hour`
— завтра) плюс дни сборки, сдача сдвигается на ближайший день из
`ship_days`, дальше путь — у Ozon календарными днями, у СДЭКа рабочими (без
субботы и воскресенья). Праздники не учитываем: в майские и новогодние
дата выйдет раньше настоящей.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.core import worktime
from app.core.config import settings

KEY = "eta"

_CARRIER = {"ozon": "Ozon", "cdek": "СДЭКа"}


def remember(details: dict, *, carrier: str, days_min: int | None, days_max: int | None,
             working: bool) -> None:
    """Запомнить срок перевозчика рядом с ценой. Нет срока — забыть прежний."""
    low, high = int(days_min or 0), int(days_max or 0)
    if not low and not high:
        details.pop(KEY, None)
        return
    low, high = low or high, high or low
    details[KEY] = {"carrier": carrier, "min": min(low, high), "max": max(low, high),
                    "working": working}


def forget(details: dict) -> None:
    details.pop(KEY, None)


def _plural(number: int) -> str:
    if number % 10 == 1 and number % 100 != 11:
        return "день"
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def _span(low: int, high: int, working: bool = False) -> str:
    numbers = str(low) if low == high else f"{low}–{high}"
    unit = _plural(high)
    if working:
        unit = "рабочий день" if unit == "день" else f"рабочих {unit}" if unit == "дней" else "рабочих дня"
    return f"{numbers} {unit}"


_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")


def _now() -> datetime:
    """Отдельной функцией — чтобы тесты подменяли часы."""
    return worktime.now_msk()


def _ship_days() -> set[int]:
    days = set()
    for part in (settings.ship_days or "").replace(" ", "").split(","):
        if part.isdigit() and 1 <= int(part) <= 7:
            days.add(int(part))
    return days or {1, 2, 3, 4, 5, 6, 7}


def _handover(start: date, handling: int) -> date:
    day = start + timedelta(days=handling)
    ship = _ship_days()
    while day.isoweekday() not in ship:
        day += timedelta(days=1)
    return day


def _travel(day: date, days: int, working: bool) -> date:
    if not working:
        return day + timedelta(days=days)
    left = days
    while left > 0:
        day += timedelta(days=1)
        if day.isoweekday() <= 5:
            left -= 1
    return day


def _parts(eta: dict | None) -> tuple[int, int, bool] | None:
    if not eta:
        return None
    try:
        return int(eta["min"]), int(eta["max"]), bool(eta.get("working"))
    except (KeyError, TypeError, ValueError):
        return None


def _handling() -> tuple[int, int]:
    low = max(0, settings.handover_days_min)
    return low, max(low, settings.handover_days)


def dates(eta: dict | None, now: datetime | None = None) -> tuple[date, date] | None:
    """Самая ранняя и самая поздняя дата получения — или None, если срока нет."""
    parts = _parts(eta)
    if parts is None:
        return None
    low, high, working = parts
    moment = worktime.to_msk(now) if now is not None else _now()
    start = moment.date() + timedelta(days=1 if moment.hour >= settings.ship_cutoff_hour else 0)
    hand_low, hand_high = _handling()
    first = _travel(_handover(start, hand_low), low, working)
    last = _travel(_handover(start, hand_high), high, working)
    return first, max(first, last)


def date_text(first: date, last: date) -> str:
    """«10 октября», «9–10 октября», «31 октября – 2 ноября»."""
    if first == last:
        return f"{first.day} {_MONTHS[first.month - 1]}"
    if first.month == last.month:
        return f"{first.day}–{last.day} {_MONTHS[last.month - 1]}"
    return f"{first.day} {_MONTHS[first.month - 1]} – {last.day} {_MONTHS[last.month - 1]}"


def by_date(eta: dict | None, now: datetime | None = None) -> str:
    """«≈ 10 октября» — или "", если срока нет или даты выключены."""
    if not settings.delivery_date_enabled:
        return ""
    span = dates(eta, now)
    return f"≈ {date_text(*span)}" if span else ""


def receive(eta: dict | None, now: datetime | None = None) -> str:
    """Для строки варианта доставки: «получите ≈ 10 октября» или прежний срок днями."""
    when = by_date(eta, now)
    if when:
        return f"получите {when}"
    return phrase_for(eta, now)


def phrase_for(eta: dict | None, now: datetime | None = None) -> str:
    """«≈ 10 октября (1 день соберём, 5 дней в пути у Ozon)» — или ""."""
    parts = _parts(eta)
    if parts is None:
        return ""
    low, high, working = parts
    hand_low, hand_high = _handling()
    carrier = _CARRIER.get(eta.get("carrier"), eta.get("carrier") or "перевозчика")
    when = by_date(eta, now)
    if when:
        return f"{when} ({_span(hand_low, hand_high)} соберём, {_span(low, high, working)} в пути у {carrier})"
    return (
        f"≈ {_span(hand_low + low, hand_high + high, working)}: "
        f"{_span(hand_low, hand_high)} соберём и сдадим, "
        f"{_span(low, high, working)} в пути у {carrier}"
    )


def phrase(details: dict | None, now: datetime | None = None) -> str:
    return phrase_for((details or {}).get(KEY), now)
