"""Цена доставки для клиента: цена перевозчика плюс надбавка на эквайринг.

ЮKassa берёт комиссию за эквайринг и фискализацию со всей суммы платежа,
включая доставку. Без надбавки клиент платил бы ровно цену перевозчика, а
комиссию с неё — магазин: на каждом заказе с платной доставкой магазин
доплачивал из своих.

Надбавка (`DELIVERY_ACQUIRING_PERCENT`) накладывается в одном месте — там,
где цена перевозчика превращается в цену для клиента
(`conversation._cdek_delivery`, `conversation._ozon_price` и плоские тарифы).
Всё дальше — сравнение перевозчиков, доплата за быстрого, «как в прошлый
раз», витрина, сводка, счёт и чек — получает уже цену с надбавкой, поэтому
расходиться ей негде. Порог бесплатной доставки работает как раньше: выше
него доставка клиенту 0, и комиссии с неё нет.

Округление вверх до копейки: недобор хотя бы копейки — та же доплата
магазина, только меньше.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, ROUND_UP, Decimal

from app.core.config import settings

_KOPECK = Decimal("0.01")


def _factor() -> Decimal:
    percent = max(0.0, float(settings.delivery_acquiring_percent or 0))
    return 1 + Decimal(str(percent)) / 100


def for_client(carrier_price: float | None) -> float | None:
    """Цена перевозчика → цена для клиента."""
    if not carrier_price:
        return carrier_price
    value = (Decimal(str(carrier_price)) * _factor()).quantize(_KOPECK, rounding=ROUND_UP)
    return float(value)


def carrier_part(client_price: float | None) -> float | None:
    """Обратно: сколько из цены для клиента — цена перевозчика.

    Нужна там, где цену сверяют с перевозчиком (Ozon пересчитывает доставку
    при создании отправления): сравнивать с ценой клиента значит каждый раз
    видеть «расхождение» ровно на надбавку.
    """
    if not client_price:
        return client_price
    value = (Decimal(str(client_price)) / _factor()).quantize(_KOPECK, rounding=ROUND_HALF_UP)
    return float(value)
