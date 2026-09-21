"""Подбор пункта выдачи Ozon и цена доставки в него.

Вынесено из диалога, потому что тем же кодом пользуется служебный эндпоинт
`/internal/ozon/quote`: каталог живёт в базе, а база — во внутренней сети, и
проверить подбор с ноутбука скриптом нельзя. Значит, проверять надо изнутри
контейнера, тем же путём, которым ходит бот.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.modules.delivery import ozon_catalog, ozon_client

logger = logging.getLogger(__name__)

# Сколько пунктов показываем клиенту. Больше — это уже не выбор, а список.
_MAX_OPTIONS = 6


def is_ready() -> bool:
    """Готовы ли мы вообще считать Ozon: без метода доставки расчёта нет."""
    return ozon_client.is_configured() and bool(settings.ozon_shipment_method_id)


async def points_for(
    city: str,
    hint: str = "",
    *,
    weight_grams: int,
    declared_value: float,
    limit: int = _MAX_OPTIONS,
) -> tuple[list, int, int]:
    """Пункты под то, что назвал клиент, и два счётчика вокруг них.

    Поиска по адресу у Ozon нет — каталог отдаётся целиком, — поэтому ищем по
    своей копии. А вот обслуживает ли пункт наш метод доставки, знает только
    Ozon: каталог общий на всю страну. Предложить пункт, на котором потом
    сорвётся расчёт, хуже, чем не предложить его вовсе.

    Счётчиков два, и оба нужны. `found` — сколько вернул каталог до проверки
    доступности: «в городе вообще нет пунктов» и «есть, но не наши» — разные
    разговоры с клиентом. `total` — сколько всего подходит под запрос, без
    ограничения по количеству: показать пять адресов из сорока и выдать их за
    весь список значит выбирать за клиента.
    """
    query = f"{city} {hint}".strip()
    rows = await ozon_catalog.find(query, limit=limit)
    if not rows:
        return [], 0, 0

    total = await ozon_catalog.count_matching(query)

    try:
        allowed = await ozon_client.available_points(
            delivery_point_ids=[row.id for row in rows],
            shipment_method_id=settings.ozon_shipment_method_id,
            weight_grams=weight_grams,
            length_mm=settings.ozon_default_length_mm,
            width_mm=settings.ozon_default_width_mm,
            height_mm=settings.ozon_default_height_mm,
            declared_value=declared_value,
        )
    except Exception:
        # Проверка не прошла — отдаём что нашли: цену всё равно считает
        # следующий вызов, и он же откажет, если пункт не подходит.
        logger.exception("Не проверили доступность пунктов Ozon в «%s»", city)
        return list(rows), len(rows), total

    return [row for row in rows if row.id in allowed], len(rows), total


async def price_for(
    point_id: int, *, phone: str, weight_grams: int, declared_value: float
) -> ozon_client.Quote:
    """Во что обойдётся доставка в конкретный пункт."""
    return await ozon_client.checkout(
        shipment_method_id=settings.ozon_shipment_method_id,
        delivery_point_id=point_id,
        phone_number=phone or settings.ozon_quote_phone,
        weight_grams=weight_grams,
        length_mm=settings.ozon_default_length_mm,
        width_mm=settings.ozon_default_width_mm,
        height_mm=settings.ozon_default_height_mm,
        declared_value=declared_value,
    )
