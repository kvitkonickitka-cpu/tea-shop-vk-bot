import contextlib
import contextvars
import json
import logging
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent / "catalog.json"

# Кто сейчас смотрит каталог: тестовый аккаунт (TEST_VK_IDS) видит и покупает
# тестовые артикулы (`shop.catalog.is_test`), остальные — нет. По умолчанию — не
# тестовый: вне хода клиента (сборка, чек, тик) тестовый артикул в каталоге есть,
# со своим GTIN, но «не в наличии», и продать его некому.
_test_viewer: contextvars.ContextVar[bool] = contextvars.ContextVar("catalog_test_viewer", default=False)


@contextlib.asynccontextmanager
async def viewer(peer_id: int):
    """На время хода клиента: тестовому аккаунту — и тестовые артикулы."""
    from app.modules.dialog import test_mode

    try:
        is_test = settings.investor_demo_enabled or await test_mode.is_test(peer_id)
    except Exception:
        logger.warning("Не проверили, тестовый ли аккаунт peer_id=%s — тестовые артикулы скрыты", peer_id, exc_info=True)
        is_test = False
    token = _test_viewer.set(is_test)
    try:
        yield
    finally:
        _test_viewer.reset(token)


def _for_viewer(items: list[dict]) -> list[dict]:
    if _test_viewer.get():
        if settings.investor_demo_enabled:
            return [{**item, "in_stock": bool(item.get("on_sale", item.get("in_stock", False)))}
                    if item.get("is_test") else item for item in items]
        return items
    return [{**item, "in_stock": False, "hidden": True} if item.get("is_test") else item for item in items]


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

        return _for_viewer(store.current_items())

    from app.modules.catalog import sheet

    from_sheet = sheet.current_items()
    if from_sheet is not None:
        return from_sheet
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def shop_closed(items: list[dict] | None = None) -> bool:
    """Продавать этому покупателю нечего: каталог есть, но в наличии ни одного артикула.

    До старта продаж так у всех, кроме тестовых аккаунтов: модель говорит, когда
    магазин открывается, а не зовёт менеджера на каждый «что есть в наличии?».
    """
    items = load_items() if items is None else items
    return bool(items) and not any(item.get("in_stock", True) for item in items)


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
    if shop_closed(items):
        # Тестовые и не готовые к продаже артикулы покупателю не называем вовсе.
        opens = settings.shop_opens_text
        return (
            f"Магазин ещё не открылся: продажи начинаются {opens}, в наличии сейчас ничего нет. "
            f"На вопросы о покупке, наличии, ценах и ассортименте отвечай, что магазин открывается {opens}, "
            "и предложи подписаться на новости сообщества, чтобы не пропустить старт. Менеджера для этого "
            "звать не нужно, заказ не оформляй. Если человек просит помочь выбрать чай по вкусу или условиям "
            "чаепития, обязательно дай полезную общую консультацию: назови 1–2 категории чая, объясни, "
            "почему они подходят его запросу, и предложи один практический приём заваривания. "
            "Это общие ориентиры, не товары в наличии и не обещание будущего ассортимента. "
            "Статус магазина вынеси в отдельный абзац после консультации и практического совета. Сообщение об открытии и предложение "
            "подписаться не должны заменять ответ на вопрос; в продолжении диалога не повторяй их без нужды. "
            "Если спросили только дату открытия или возможность покупки, отвечай по этому вопросу без лекции о чае."
        )
    items = [item for item in items if not item.get("hidden")]
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
        # Без заварки в промпте модель на «как заваривать?» звала менеджера,
        # хотя ответ лежит в каталоге (24.10.2026).
        brewing = " ".join((item.get("brewing") or "").split())
        if brewing:
            line += f" Как заваривать: {brewing}"
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
    if settings.investor_demo_enabled:
        lines.insert(0, "Демонстрационный режим: доступны тестовые товары из списка. "
                     "Оформляй заказ обычными инструментами, соблюдай согласие на ПДн. "
                     "Текущий каталог главнее старых сообщений об отсутствии продаж. "
                     "Не повторяй старые сообщения о дате открытия. "
                     "Оплата и доставка работают через настоящие сервисы; не называй их имитацией.")
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
