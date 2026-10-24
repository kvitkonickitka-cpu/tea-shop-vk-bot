import json
import logging
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent / "catalog.json"


def from_db() -> bool:
    """Каталог ведётся в базе (`CATALOG_SOURCE=db`), а не в Google Таблице."""
    return settings.catalog_source.strip().lower() == "db"


def load_items() -> list[dict]:
    """Ассортимент: из базы, из Google Таблицы или из образа.

    В режиме базы пустой список — пауза (`paused`), а не повод взять
    таблицу или `catalog.json`.
    """
    if from_db():
        from app.modules.catalog import store

        return store.current_items()

    from app.modules.catalog import sheet

    from_sheet = sheet.current_items()
    if from_sheet is not None:
        return from_sheet
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


async def ensure_fresh() -> None:
    """Подтянуть в память контейнера текущую версию каталога. Не бросает."""
    if from_db():
        from app.modules.catalog import store

        await store.ensure_fresh()
        return
    from app.modules.catalog import sheet

    await sheet.ensure_fresh()


def paused() -> bool:
    """Продавать нечего: каталог в базе пуст или не читается."""
    if not from_db():
        return False
    from app.modules.catalog import store

    return store.unavailable()


def sku_ref(item: dict | None) -> dict:
    """{"sku_id": …} для позиции заказа, если товар из каталога в базе.

    Артикул в позиции нужен складу: по нему резервируется сырьё (у набора — по
    составу). У товара из Google Таблицы артикула нет — позиция как раньше.
    """
    sku = (item or {}).get("sku_id")
    return {"sku_id": sku} if sku else {}


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


_SET_WORDS = {"set", "сет", "набор"}


def kind_for(name: str, items: list[dict] | None = None) -> str:
    """Тип позиции: `set` — сет или набор в коробке со своим кодом, иначе `pack`."""
    from app.modules.marking.models import PACK, SET

    item = find_item(name, items) or {}
    return SET if str(item.get("kind", "")).strip().casefold() in _SET_WORDS else PACK


def item_by_gtin(gtin: str, items: list[dict] | None = None) -> dict | None:
    """Товар каталога с этим GTIN — чтобы назвать сборщику, что он отсканировал."""
    from app.modules.marking import codes

    items = load_items() if items is None else items
    for item in items:
        if gtin and codes.normalize_gtin(item.get("gtin", "")) == gtin:
            return item
    return None


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

    # «С чем советуем» — только то, что есть в наличии: сорт, которого нет,
    # модель предлагала сама (24.10.2026, Да Хун Пао к Те Гуань Иню), хотя
    # допродажа кода его пропускала.
    available = {item["name"].casefold() for item in items if item.get("in_stock", True)}
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
        recommended = [name for name in item.get("recommended") or [] if name.casefold() in available]
        if recommended:
            line += f" С чем советуем: {', '.join(recommended)}."
        lines.append(line)

    # Чего нет — только названиями: иначе на «а сет … есть?» модель не знает
    # такого товара и зовёт менеджера, а ответ простой — сейчас нет.
    missing = [item["name"] for item in items if not item.get("in_stock", True)]
    if missing:
        lines.append(
            "Сейчас нет в наличии (заказать нельзя; спросят — скажи, что сейчас нет, и предложи похожее "
            f"из списка выше): {', '.join(missing)}."
        )
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
