"""Скидка по коду друга в ценах позиций — так чек, платёж и закрывающий чек сходятся сами.

В чеке ЮKassa строки «скидка» нет: скидка распределяется по товарным
позициям пропорционально их сумме, до копеек, остаток копеек — на последнюю
позицию. Если сумма строки после скидки не делится на количество без
остатка, строка делится на две: N−1 штук по одной цене и 1 штука с остатком
копеек. Доставка скидки не получает.

Прайс позиции хранится в `list_price`, цена со скидкой — в `price`: так
`items_total` — уже сумма к оплате за товары, порог бесплатной доставки
считается по сумме после скидки, и всё, что дальше читает позиции (счёт,
заказ, аналитика, сборка с кодами), видит настоящие цены продажи.
`reprice` вызывается при каждой записи черновика (`state.set_draft`):
состав поменялся — скидка пересчитана заново от прайса.

Параметры скидки лежат в `details.referral`: процент, потолок в рублях,
минимальная сумма товаров. Решает только код; модель скидку не считает.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

_KEEP = ("list_price", "_part")


def _cents(value) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _rub(cents: int) -> float:
    return float(Decimal(cents) / 100)


def merge(items: list[dict]) -> list[dict]:
    """Позиции по прайсу: части разделённой строки — обратно в одну, цена — прайсовая."""
    result: list[dict] = []
    for item in items:
        price = item.get("list_price", item.get("price"))
        base = {k: v for k, v in item.items() if k not in _KEEP}
        base["price"] = price
        if item.get("_part") and result and result[-1]["name"] == base["name"] \
                and float(result[-1]["price"]) == float(price):
            result[-1]["quantity"] = int(result[-1]["quantity"]) + int(item.get("quantity") or 1)
            continue
        result.append(base)
    return result


def discount_for(list_total_cents: int, *, percent, cap, min_total) -> int:
    if list_total_cents <= 0 or list_total_cents < _cents(min_total or 0):
        return 0
    raw = (Decimal(list_total_cents) * Decimal(str(percent)) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if cap:
        raw = min(raw, Decimal(_cents(cap)))
    return int(raw)


def distribute(items: list[dict], discount_cents: int) -> list[dict]:
    """Скидка по позициям пропорционально, остаток — на последнюю; строки делятся до копеек."""
    if discount_cents <= 0:
        return [dict(item) for item in items]
    lines = [_cents(item["price"]) * int(item.get("quantity") or 1) for item in items]
    total = sum(lines)
    shares = []
    for i, line in enumerate(lines):
        if i == len(lines) - 1:
            shares.append(discount_cents - sum(shares))
        else:
            # Вниз до копейки: весь остаток копеек — последней позиции.
            shares.append(int((Decimal(discount_cents) * line / total).quantize(Decimal(1), rounding=ROUND_DOWN)))
    result: list[dict] = []
    for item, line, share in zip(items, lines, shares):
        quantity = int(item.get("quantity") or 1)
        net = line - share
        unit, rest = divmod(net, quantity)
        common = {**item, "list_price": item["price"]}
        if rest == 0:
            result.append({**common, "price": _rub(unit), "quantity": quantity})
            continue
        if quantity > 1:
            result.append({**common, "price": _rub(unit), "quantity": quantity - 1})
            result.append({**common, "price": _rub(unit + rest), "quantity": 1, "_part": 1})
        else:  # pragma: no cover — у одной штуки остатка нет
            result.append({**common, "price": _rub(net), "quantity": 1})
    return result


def reprice(draft) -> None:
    """Пересчитать цены позиций черновика по `details.referral`. Без скидки — прайс."""
    base = merge(draft.items or [])
    terms = (draft.details or {}).get("referral")
    if not terms:
        draft.items = base
        draft.items_total = _rub(sum(_cents(i["price"]) * int(i.get("quantity") or 1) for i in base))
        return
    list_total = sum(_cents(i["price"]) * int(i.get("quantity") or 1) for i in base)
    discount = discount_for(list_total, percent=terms.get("percent", 0), cap=terms.get("cap"),
                            min_total=terms.get("min_total"))
    terms["list_total"] = _rub(list_total)
    terms["discount"] = _rub(discount)
    terms["active"] = discount > 0
    draft.items = distribute(base, discount)
    draft.items_total = _rub(list_total - discount)
