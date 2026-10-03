import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent / "catalog.json"


def load_items() -> list[dict]:
    """Ассортимент: из Google Таблицы, если она подключена и прочитана, иначе из образа."""
    from app.modules.catalog import sheet

    from_sheet = sheet.current_items()
    if from_sheet is not None:
        return from_sheet
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def find_item(name: str, items: list[dict] | None = None) -> dict | None:
    """Товар каталога по названию из заказа.

    Сначала точное совпадение, потом вхождение — тем же правилом, каким
    `propose_order` сопоставляет названное клиентом с каталогом: в заказе
    лежит название из каталога, а у заказа из витрины — название из ВК.
    """
    items = load_items() if items is None else items
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    for item in items:
        if item.get("name", "").strip().casefold() == wanted:
            return item
    for item in items:
        have = item.get("name", "").strip().casefold()
        if have and (wanted in have or have in wanted):
            return item
    return None


def gtin_for(name: str, items: list[dict] | None = None) -> str:
    """GTIN товара из «Честного знака» — его задаёт владелец в каталоге.

    Пусто — GTIN не задан или записан с ошибкой: собрать такой товар с
    кодами нельзя, и страница сборки скажет об этом прямо.
    """
    from app.modules.marking import codes

    item = find_item(name, items)
    gtin = codes.normalize_gtin((item or {}).get("gtin", ""))
    return gtin if codes.gtin_is_valid(gtin) else ""


def marking_configured(items: list[dict] | None = None) -> bool:
    """Задан ли GTIN хоть у одного товара — значит, собираем с кодами."""
    items = load_items() if items is None else items
    return any(gtin_for(item.get("name", ""), items) for item in items)


async def build_catalog_context() -> str:
    try:
        items = load_items()
    except Exception:
        logger.exception("Failed to load catalog.json")
        return ""

    lines = []
    for item in items:
        if not item.get("in_stock", True):
            continue

        line = f"- {item['name']} ({item['price']} ₽"
        package_sizes = item.get("package_sizes")
        if package_sizes:
            line += f", упаковки: {', '.join(package_sizes)}"
        line += f"): {item.get('description', '')}"
        link = item.get("link")
        if link:
            line += f" Ссылка на товар (в т.ч. с фото): {link}"
        synonyms = item.get("synonyms") or []
        if synonyms:
            line += f" Ещё называют: {', '.join(synonyms)}."
        recommended = item.get("recommended") or []
        if recommended:
            line += f" С чем советуем: {', '.join(recommended)}."
        lines.append(line)

    return "\n".join(lines)


def upsell_for(draft_items: list[dict], items: list[dict] | None = None) -> dict | None:
    """Одна позиция для допродажи — или None.

    Только из столбца «С чем советуем» у товаров черновика: первая, которая
    есть в наличии и которой ещё нет в черновике. Пустой столбец — ничего не
    предлагаем: придумывать допродажу модели нельзя.
    """
    items = load_items() if items is None else items
    in_draft = {str(row.get("name", "")).casefold() for row in draft_items}
    for row in draft_items:
        product = find_item(str(row.get("name", "")), items)
        for name in (product or {}).get("recommended") or []:
            candidate = find_item(name, items)
            if (
                candidate
                and candidate.get("in_stock", True)
                and candidate["name"].casefold() not in in_draft
            ):
                return candidate
    return None


def cheapest_in_stock_price(items: list[dict] | None = None) -> float | None:
    """Цена самой дешёвой пачки в наличии — мерка «можно добавить ещё пачку»."""
    items = load_items() if items is None else items
    prices = [float(item["price"]) for item in items if item.get("in_stock", True)]
    return min(prices) if prices else None
