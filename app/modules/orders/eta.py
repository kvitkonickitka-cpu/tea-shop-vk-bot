"""Срок доставки для клиента: сколько соберём и сдадим плюс сколько едет.

Перевозчик считает срок от сдачи посылки, а сдаём мы её не сразу — после
оплаты обещаем «в течение 1–2 дней» (`handover_promise`). Клиент, которому
назвали «5 дней» по Ozon, ждал бы 6–7. Поэтому срок называем целиком и
раскладываем, из чего он сложился:

    ≈ 6–7 дней: 1–2 дня соберём и сдадим, 5 дней в пути у Ozon

Срок перевозчика приходит вместе с ценой — у СДЭКа в тарифе (рабочие дни),
у Ozon в расчёте (дни). Он хранится в черновике (`details.eta`) рядом с
ценой и обновляется при каждом её пересчёте: ответ инструмента в историю
диалога не попадает, а спросить «сколько ехать?» клиент может и через пару
сообщений. Нет срока у перевозчика — нет его и у нас: не выдумываем.
"""

from __future__ import annotations

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


def phrase_for(eta: dict | None) -> str:
    """«≈ 6–7 дней: 1–2 дня соберём и сдадим, 5 дней в пути у Ozon» — или ""."""
    if not eta:
        return ""
    try:
        low, high, working = int(eta["min"]), int(eta["max"]), bool(eta.get("working"))
    except (KeyError, TypeError, ValueError):
        return ""
    hand_low = max(0, settings.handover_days_min)
    hand_high = max(hand_low, settings.handover_days)
    carrier = _CARRIER.get(eta.get("carrier"), eta.get("carrier") or "перевозчика")
    return (
        f"≈ {_span(hand_low + low, hand_high + high, working)}: "
        f"{_span(hand_low, hand_high)} соберём и сдадим, "
        f"{_span(low, high, working)} в пути у {carrier}"
    )


def phrase(details: dict | None) -> str:
    return phrase_for((details or {}).get(KEY))
