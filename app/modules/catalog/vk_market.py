"""Товары магазина сообщества: чтение витрины ВК.

Ассортимент бота сейчас лежит в `catalog.json` внутри образа: цену и
наличие правят руками и деплоем. В самой группе всё это уже есть — и
описание, и цена, и признак доступности, — поэтому витрину можно читать
напрямую тем же токеном сообщества, которым мы забираем заказы.

Пока это только разведка: `probe()` показывает, что на самом деле лежит в
магазине и в каких полях, чтобы схему зеркала проектировать по живым
данным, а не по документации. Ничего не пишем и ничего не меняем.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

VK_API_URL = "https://api.vk.com/method"
# Больше двухсот ВК за раз не отдаёт.
_PAGE = 200
_TIMEOUT_SECONDS = 10

# Что означает availability в ответе ВК.
AVAILABILITY = {0: "в продаже", 1: "удалён", 2: "недоступен"}


def owner_id() -> int:
    """Владелец витрины: у группы это её id со знаком минус.

    В настройках id живёт как «club240363526» или просто числом — берём
    цифры, а знак ставим сами.
    """
    digits = "".join(ch for ch in (settings.vk_group_id or "") if ch.isdigit())
    if not digits:
        raise RuntimeError("VK_GROUP_ID не задан — не знаем, чью витрину читать")
    return -int(digits)


def token() -> str:
    """Чем читаем витрину.

    Токеном сообщества `market.get` не работает: ВК отвечает «27 Group
    authorization failed: method is unavailable with group auth» — это не
    про права, витрину группе смотреть не разрешают в принципе. Заказы тем
    же токеном читаются, так что дело не в нём.

    Поэтому витрину читает токен администратора сообщества
    (`VK_USER_TOKEN`), а групповой остаётся на всё остальное.
    """
    return settings.vk_user_token or settings.vk_access_token


def token_kind() -> str:
    return "администратора" if settings.vk_user_token else "сообщества"


def is_configured() -> bool:
    return bool(token() and settings.vk_group_id)


def price_rubles(price) -> float:
    """Цена ВК в рублях: в API она приходит копейками и строкой."""
    if isinstance(price, dict):
        price = price.get("amount", 0)
    try:
        return round(float(price) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


async def get_items(count: int = _PAGE, offset: int = 0) -> dict:
    """Сырой ответ `market.get` — как его отдал ВК, без разбора."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{VK_API_URL}/market.get",
            params={
                "owner_id": owner_id(),
                "count": min(count, _PAGE),
                "offset": offset,
                # extended даёт описание, фотографии и свойства вариантов —
                # ровно то, из-за чего мы сюда и пришли.
                "extended": 1,
                "access_token": token(),
                "v": settings.vk_api_version,
            },
        )
        data = response.json()
        if "error" in data:
            error = data["error"]
            raise RuntimeError(
                "VK API error (market.get): "
                f"{error.get('error_code')} {error.get('error_msg')}"
            )
        return data.get("response") or {}


def _describe(item: dict) -> dict:
    """Товар в том виде, в каком он нужен для схемы зеркала."""
    description = (item.get("description") or "").strip()
    return {
        "id": item.get("id"),
        "название": item.get("title"),
        "цена": price_rubles(item.get("price")),
        "доступность": AVAILABILITY.get(item.get("availability"), item.get("availability")),
        "остаток": item.get("stock_amount", "учёт выключен"),
        "ссылка": item.get("url"),
        "фото": len(item.get("photos") or []) or bool(item.get("thumb_photo")),
        "sku": item.get("sku") or "",
        # Фасовки в ВК заводят по-разному: вариантами одного товара,
        # отдельными товарами или просто словами в описании. От этого
        # зависит, сможет ли бот назвать цену за нужную фасовку.
        "свойства": item.get("property_values") or [],
        "группа вариантов": item.get("variants_grouping_id"),
        "главный вариант": item.get("is_main_variant"),
        "описание": description[:400] + ("…" if len(description) > 400 else ""),
        "длина описания": len(description),
    }


async def probe(limit: int = 5) -> dict:
    """Что лежит в витрине: сводка по всем товарам и разбор первых.

    Отвечает на вопросы, которые по документации не решить: заведены ли
    фасовки вариантами, включён ли учёт остатков, есть ли у товаров
    описания и ссылки.
    """
    if not is_configured():
        return {"error": "нет токена или VK_GROUP_ID"}

    response = await get_items(count=_PAGE)
    items = response.get("items") or []

    fields: set[str] = set()
    by_availability: dict[str, int] = {}
    with_stock = with_properties = with_description = with_url = 0
    grouping: set = set()

    for item in items:
        fields.update(item.keys())
        label = str(AVAILABILITY.get(item.get("availability"), item.get("availability")))
        by_availability[label] = by_availability.get(label, 0) + 1
        if item.get("stock_amount") is not None:
            with_stock += 1
        if item.get("property_values"):
            with_properties += 1
        if (item.get("description") or "").strip():
            with_description += 1
        if item.get("url"):
            with_url += 1
        if item.get("variants_grouping_id"):
            grouping.add(item["variants_grouping_id"])

    return {
        "витрина": owner_id(),
        "читали токеном": token_kind(),
        "всего товаров в магазине": response.get("count"),
        "получено за один запрос": len(items),
        "по доступности": by_availability,
        "с остатком (stock_amount)": with_stock,
        "со свойствами (фасовки вариантами)": with_properties,
        "групп вариантов": len(grouping),
        "с описанием": with_description,
        "со ссылкой": with_url,
        "поля, которые встречаются": sorted(fields),
        "товары": [_describe(item) for item in items[:limit]],
    }
