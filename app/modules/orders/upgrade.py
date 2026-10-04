"""Самая дешёвая доставка — первой, быстрая — второй строкой, с доплатой.

Раньше бот называл тот вариант, о котором спросила модель, а порог
бесплатной доставки гасил цену любого перевозчика целиком: клиенту выше
порога было всё равно, Ozon или СДЭК, и магазин платил за СДЭК вдвое
больше. Теперь (`DELIVERY_UPGRADE_PRICING_ENABLED`):

- оба перевозчика считаются до города, первым называется самый дешёвый
  (при равной цене — Ozon), второй — одной строкой и только если он
  привозит хотя бы на день раньше;
- порог покрывает самый дешёвый вариант. Выбрал дороже — клиент доплачивает
  разницу (`delivery_cost` = выбранный − дешёвый, не меньше нуля), а
  настоящая цена перевозчика, как и раньше, — в `details.carrier_delivery_cost`;
- «дешёвый» — среди того, что действительно посчиталось: второй перевозчик
  не ответил — сравнивать не с чем, выше порога доставка бесплатная;
- расчёты лежат в `details.carrier_quotes` и перед счётом пересчитываются
  вместе с выбранной доставкой, если устарели: изменилась доплата — изменился
  итог, и счёт без вопроса не выставляется (правило `seen_total`).

Клиенту — одна фраза (`templates.delivery_options`): «Ozon — 117 ₽, получите
≈ 10 октября. Нужно быстрее — СДЭК 245 ₽, получите ≈ 8 октября».
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from app.core.config import free_delivery_threshold, settings
from app.modules.orders import eta

logger = logging.getLogger(__name__)

KEY = "carrier_quotes"
SURCHARGE = "delivery_surcharge"

_NAMES = {"ozon": "Ozon", "cdek": "СДЭК"}
_METHODS = {"ozon": "ozon_pvz", "cdek": "cdek_pvz"}
_ALTERNATIVE_TIMEOUT = 12


def is_enabled() -> bool:
    return bool(settings.delivery_upgrade_pricing_enabled)


def carrier_of(method: str | None) -> str | None:
    if method == "ozon_pvz":
        return "ozon"
    if method in ("cdek_pvz", "cdek_courier"):
        return "cdek"
    return None


def quote(carrier: str, cost: float, eta_: dict | None) -> dict:
    return {"carrier": carrier, "name": _NAMES[carrier], "method": _METHODS[carrier],
            "cost": round(float(cost), 2), "eta": eta_ or None}


def remember(details: dict, city: str, quotes: list[dict]) -> None:
    """Запомнить расчёты до города. Город другой — прежние расчёты не в счёт."""
    held = details.get(KEY) or {}
    if (held.get("city") or "").casefold() != (city or "").casefold():
        held = {"city": city, "quotes": {}}
    for row in quotes:
        held["quotes"][row["carrier"]] = dict(row, at=time.time())
    details[KEY] = held


def quotes_for(details: dict, city: str | None = None) -> list[dict]:
    held = (details or {}).get(KEY) or {}
    if city is not None and (held.get("city") or "").casefold() != (city or "").casefold():
        return []
    return list((held.get("quotes") or {}).values())


def _last_date(row: dict) -> date | None:
    span = eta.dates(row.get("eta"))
    return span[1] if span else None


def cheapest(rows: list[dict]) -> dict | None:
    """Самый дешёвый; при равной цене — Ozon, потом кто раньше привезёт."""
    if not rows:
        return None
    return min(rows, key=lambda r: (round(r["cost"], 2), r["carrier"] != "ozon",
                                    _last_date(r) or date.max))


def faster(rows: list[dict]) -> dict | None:
    """Второй вариант — только если привозит хотя бы на день раньше дешёвого."""
    base = cheapest(rows)
    if base is None:
        return None
    base_day = _last_date(base)
    best = None
    for row in rows:
        if row is base:
            continue
        day = _last_date(row)
        if base_day is None or day is None or (base_day - day).days < 1:
            continue
        if best is None or day < _last_date(best):
            best = row
    return best


def surcharge(details: dict, method: str | None, real_cost: float) -> float | None:
    """Доплата за выбранного перевозчика сверх самого дешёвого.

    None — сравнивать не с чем (посчитан только один перевозчик). 0 — выбран
    самый дешёвый. Свой перевозчик сравнивается только с другим: Ozon в
    соседний пункт на 4 ₽ дороже первого — не повод для доплаты.
    """
    rows = quotes_for(details)
    mine = carrier_of(method)
    if not is_enabled() or mine is None or not rows:
        return None
    base = cheapest(rows)
    if method == "cdek_courier":
        # Курьер дороже любого пункта: доплата — от самого дешёвого пункта.
        return max(0.0, round(float(real_cost) - base["cost"], 2))
    if len(rows) < 2:
        return None
    if base["carrier"] == mine:
        return 0.0
    return max(0.0, round(float(real_cost) - base["cost"], 2))


def is_free() -> bool:
    return free_delivery_threshold() is not None


def above_threshold(items_total: float) -> bool:
    threshold = free_delivery_threshold()
    return threshold is not None and items_total >= threshold


def client_price(row: dict, base: dict | None, free: bool) -> float:
    """Сколько клиент заплатит за этот вариант."""
    if not free:
        return row["cost"]
    if base is None or row is base or row["carrier"] == base["carrier"]:
        return 0.0
    return max(0.0, round(row["cost"] - base["cost"], 2))


async def quote_city(draft, city: str, street: str = "", only: set[str] | None = None) -> list[dict]:
    """Ozon и СДЭК (пункт выдачи) до города — параллельно; кто не ответил, того нет."""
    from app.modules.delivery import ozon_quote
    from app.modules.orders import conversation

    async def ozon() -> dict | None:
        if not ozon_quote.is_ready():
            return None
        picked = await conversation._ozon_points(draft, city, street)
        if not picked.points:
            return None
        found = await conversation._ozon_price(draft, int(picked.points[0].id))
        return quote("ozon", found.total,
                     {"carrier": "ozon", "min": found.days, "max": found.days, "working": False})

    async def cdek() -> dict | None:
        tariff, total = await conversation._cdek_delivery(draft, "cdek_pvz", city)
        return quote("cdek", total, {"carrier": "cdek", "min": tariff.period_min,
                                     "max": tariff.period_max, "working": True})

    async def safe(job, name):
        try:
            return await asyncio.wait_for(job(), timeout=_ALTERNATIVE_TIMEOUT)
        except Exception as error:
            # Город в лог не пишем: вместе с peer_id это уже почти адрес.
            logger.warning("Доставка %s до города не посчитана — %s", name, type(error).__name__)
            return None

    jobs = [(ozon, "Ozon", "ozon"), (cdek, "СДЭК", "cdek")]
    jobs = [job for job in jobs if only is None or job[2] in only]
    return [row for row in await asyncio.gather(*(safe(job, name) for job, name, _ in jobs)) if row]


def _stale(row: dict) -> bool:
    return time.time() - float(row.get("at") or 0) > settings.delivery_quote_ttl_minutes * 60


async def compare(draft, city: str, method: str) -> None:
    """После расчёта выбранного перевозчика — досчитать второго до того же города.

    Свой расчёт кладём как есть; второго перевозчика считаем, только если
    свежего расчёта до этого города нет. Курьер — не пункт выдачи: его цену
    в расчёты до города не кладём и заново ничего не считаем (вместо города
    у него полный адрес) — сравниваем с тем, что уже посчитано.
    """
    if not is_enabled() or not city or method == "cdek_courier":
        return
    own = own_quote(draft)
    if own is None:
        return
    remember(draft.details, city, [own])
    other = "cdek" if own["carrier"] == "ozon" else "ozon"
    held = {row["carrier"]: row for row in quotes_for(draft.details, city)}
    if other in held and not _stale(held[other]):
        return
    remember(draft.details, city, await quote_city(draft, city, only={other}))


async def refresh(draft) -> None:
    """Перед счётом: самый дешёвый расчёт устарел — пересчитать его.

    Свой перевозчик пересчитан выше, вместе с выбранным пунктом; здесь —
    только тот, с кем сравниваем, иначе доплата считалась бы по старой цене.
    """
    if not is_enabled():
        return
    details = draft.details
    city = (details.get(KEY) or {}).get("city")
    mine = carrier_of(draft.delivery_method)
    rows = quotes_for(details)
    if not city or mine is None or len(rows) < 2:
        return
    own = own_quote(draft)
    if own is not None and draft.delivery_method != "cdek_courier":
        remember(details, city, [own])
    others = {row["carrier"] for row in rows if row["carrier"] != mine and _stale(row)}
    if others:
        fresh = await quote_city(draft, city, only=others)
        if fresh:
            remember(details, city, fresh)


def own_quote(draft) -> dict | None:
    """Расчёт выбранного перевозчика из черновика — для сравнения с другим."""
    carrier = carrier_of(draft.delivery_method)
    if carrier is None or draft.delivery_cost is None:
        return None
    real = draft.details.get("carrier_delivery_cost", draft.delivery_cost)
    return quote(carrier, real, draft.details.get(eta.KEY))


def options_note(draft) -> str:
    """Подсказка модели: варианты до города одной фразой, дешёвый — первым."""
    from app.messages import templates

    if not is_enabled() or draft.delivery_method == "cdek_courier":
        return ""
    rows = quotes_for(draft.details)
    if len(rows) < 2:
        return ""
    base, fast = cheapest(rows), faster(rows)
    free = above_threshold(draft.items_total)
    mine = carrier_of(draft.delivery_method)
    line = templates.delivery_options(
        base_name=base["name"], base_price=client_price(base, base, free), base_when=eta.receive(base["eta"]),
        fast_name=fast["name"] if fast else "", fast_price=client_price(fast, base, free) if fast else 0,
        fast_when=eta.receive(fast["eta"]) if fast else "", free=free,
    )
    if mine == base["carrier"]:
        return (
            f"Варианты доставки до города — назови клиенту этой фразой, дешёвый первым: «{line}». "
            + (f"Выберет {fast['name']} — вызови set_delivery_method с method={fast['method']}. " if fast else "")
        )
    gap = round(own_quote(draft)["cost"] - base["cost"], 2) if own_quote(draft) else 0
    return (
        f"Клиент выбрал {_NAMES[mine]}, а дешевле {base['name']} — на {templates.amount(gap)} ₽ "
        f"({eta.receive(base['eta']) or 'срок не известен'}). Скажи об этом одной фразой, решает клиент; "
        f"захочет {base['name']} — вызови set_delivery_method с method={base['method']}. "
    )


def upgraded_by(draft) -> float | None:
    """Выбран не самый дешёвый перевозчик — на сколько дороже. Для журнала воронки."""
    rows = quotes_for(draft.details)
    mine = carrier_of(draft.delivery_method)
    if not is_enabled() or mine is None or len(rows) < 2:
        return None
    base = cheapest(rows)
    if base["carrier"] == mine and draft.delivery_method != "cdek_courier":
        return None
    real = draft.details.get("carrier_delivery_cost", draft.delivery_cost) or 0
    return max(0.0, round(float(real) - base["cost"], 2))
