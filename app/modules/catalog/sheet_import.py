"""Разовый перенос каталога из Google Таблицы в базу (`shop.catalog`).

    scripts/api.sh catalog/import-from-sheet            # отчёт, база не меняется
    scripts/api.sh 'catalog/import-from-sheet?apply=1'  # записать

Таблица только читается. Источники:

- «Каталог SKU» (вкладка `CATALOG_SHEET_TAB`) — то, что бот продаёт сейчас:
  тексты, ссылки, GTIN, «в наличии», «новинка», «с чем советуем»;
- «Товары» — артикулы (`sku_id` = «Код»), тип, вес, название, связь с
  каталогом бота через «Название в каталоге бота»;
- «Состав» — из чего собраны сеты и наборы.

Справочники источником правды не становятся. Цена — из финмодели
(`fin.sku`, `fin.bundles`): в «Каталоге SKU» тестовые 1 500 ₽, в отчёт идут
обе. «Себестоимость» и «Приходы» не переносятся вовсе, «Настройки» только
сравниваются с `fin.inputs` — вводные живут там.

Записываются только артикулы, которых в базе ещё нет: повторный запуск не
затрёт то, что владелец уже поправил в Metabase. В продажу (`on_sale`) не
включается ничего без GTIN — так держит и сама база.

Пакетик внутри набора — отдельный артикул: «Состав» говорит «35 г
P-DIANHONG-100», а у пакетика 35 г свой GTIN и своя этикетка. Импорт
заводит его как `P-DIANHONG-35` (не в продаже, без цены) и кладёт в состав.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text

from app.core import google_sheets
from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.analytics import references
from app.modules.catalog import schema, sheet

logger = logging.getLogger(__name__)

PRODUCTS_TAB = references.TABS["products"]
COMPOSITION_TAB = references.TABS["composition"]
SETTINGS_TAB = references.TABS["settings"]

AUTHOR = "импорт из таблицы"

# Ключи «Настроек» и их пары в fin.inputs. Ключа нет в словаре — ищем
# такой же ключ в fin.inputs.
SETTINGS_TO_INPUTS = {
    "order_other_variable_rub": "other_variable_per_order",
    "doypack_rub": "pouch_100g_cost",
    "minibag_35g_rub": "pouch_35g_cost",
    "label_rub": "pack_label_cost",
    "marking_code_rub": "code_price",
    "extra_codes_per_set_box": "kin_codes_per_box",
    "acquiring_cards_rate": "acquiring_rate",
    "acquiring_vat_rate": "vat_on_fee",
    "fiscalization_rate": "fiscal_rate",
    "usn_reserve_rate": "usn_rate",
    "inbound_logistics_rub_per_kg": "inbound_logistics_per_kg",
    "free_delivery_cost_rub": "free_delivery_cost",
    "usd_rate": "usd_rub",
    "usd_rub_rate": "usd_rub",
}
# Эта настройка — не вводная финмодели, а настройка бота.
_BOT_SETTINGS = {"free_delivery_threshold_rub": "FREE_DELIVERY_THRESHOLD"}

_NOT_IN_CATALOG = references.NOT_IN_CATALOG
_TEMPERATURE = re.compile(r"(\d{2,3})\s*[-–—]\s*(\d{2,3})\s*°")
_BREWING_HEADING = re.compile(r"(?:^|\n|\.\s)\s*Как заваривать\s*:?", re.I)
_SUPPLIER_CODE = re.compile(r"\(([^()]+)\)\s*$")
_VK_MARKET = re.compile(r"^https://(m\.)?vk\.(ru|com)/market")


@dataclass
class Plan:
    """Что записать и что сказать владельцу перед записью."""

    items: list[dict] = field(default_factory=list)
    bundle_items: list[dict] = field(default_factory=list)
    # Разделы отчёта: заголовок → строки.
    report: dict[str, list[str]] = field(default_factory=dict)

    def note(self, section: str, line: str) -> None:
        self.report.setdefault(section, []).append(line)


def _fold(value: str) -> str:
    return " ".join((value or "").replace("ё", "е").replace("Ё", "Е").split()).casefold()


def _catalog_rows(rows: list[list[str]]) -> list[dict]:
    """«Каталог SKU» как словари по ключам разбора бота (`sheet._COLUMNS`)."""
    if not rows:
        return []
    header = [sheet._COLUMNS.get(cell.strip().lower()) for cell in rows[0]]
    result = []
    for number, row in enumerate(rows[1:], start=2):
        values = {key: (row[i] if i < len(row) else "") for i, key in enumerate(header) if key}
        if any((value or "").strip() for value in values.values()):
            values["_row"] = number
            result.append(values)
    return result


def split_brewing(description: str) -> tuple[str, str]:
    """Описание без блока «Как заваривать» и сам блок (или пусто)."""
    match = _BREWING_HEADING.search(description or "")
    if not match:
        return (description or "").strip(), ""
    start = match.start() + (1 if description[match.start()] in ".\n" else 0)
    return description[:start].strip(), description[match.end():].strip()


def _temperatures(textual: str) -> set[tuple[int, int]]:
    return {(int(a), int(b)) for a, b in _TEMPERATURE.findall(textual or "")}


def _number(raw: str) -> Decimal | None:
    try:
        return references.number(raw, percent_ok=True)
    except references.BadValue:
        return None


def _yes(raw: str, default: bool) -> bool:
    value = (raw or "").strip().casefold()
    if not value:
        return default
    return value in sheet._YES


def _list(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;,]", raw or "") if part.strip()]


def _match_raw(product: dict, fin_skus: list[dict]) -> dict | None:
    """Сырьё финмодели для пачки или блина: по артикулу поставщика, потом по названию."""
    supplier = (product.get("supplier_sku") or "").strip().casefold()
    if supplier:
        for sku in fin_skus:
            found = _SUPPLIER_CODE.search(sku["sku_name"])
            if found and found.group(1).strip().casefold() == supplier:
                return sku
    name = _fold(product.get("name", ""))
    if name:
        for sku in fin_skus:
            fin_name = _fold(_SUPPLIER_CODE.sub("", sku["sku_name"]))
            if name in fin_name or fin_name in name:
                return sku
    return None


def _component_code(code: str, grams: int) -> str:
    """P-DIANHONG-100 + 35 г → P-DIANHONG-35."""
    base = re.sub(r"-\d+$", "", code)
    return f"{base}-{grams}"


def build_plan(
    tabs: dict[str, list[list[str]]],
    fin_skus: list[dict],
    fin_bundles: list[dict],
    inputs: dict[str, Decimal | str | None],
) -> Plan:
    """Таблица и финмодель → артикулы, состав и отчёт. Без базы и сети."""
    plan = Plan()
    catalog = _catalog_rows(tabs.get("catalog") or [])
    products_tab = references.read_tab("products", tabs.get("products") or [])
    composition_tab = references.read_tab("composition", tabs.get("composition") or [])
    settings_tab = references.read_tab("settings", tabs.get("settings") or [])
    for tab in (products_tab, composition_tab, settings_tab):
        if tab.problem:
            plan.note("Вкладки не прочитаны", tab.problem)

    by_bot_name = {_fold(row.get("name", "")): row for row in catalog if row.get("name", "").strip()}
    linked: set[str] = set()
    products: dict[str, dict] = {}
    for number, product in products_tab.rows:
        code = (product.get("code") or "").strip()
        if not code:
            plan.note("Пусто", f"«{PRODUCTS_TAB}», строка {number}: нет «Кода»")
            continue
        if code in products:
            plan.note("Не прошло проверки", f"«{PRODUCTS_TAB}»: код {code} повторяется (строка {number})")
            continue
        product["_row"] = number
        products[code] = product

    fin_by_bundle = {_fold(b["bundle"]): b for b in fin_bundles}
    matched_fin: set[str] = set()
    matched_bundles: set[str] = set()
    names_to_sku: dict[str, str] = {}
    pending_recommend: dict[str, list[str]] = {}

    for order, (code, product) in enumerate(products.items(), start=1):
        where = f"{code} (строка {product['_row']})"
        kind = (product.get("kind") or "").strip().casefold()
        if kind not in schema.KINDS:
            plan.note("Не прошло проверки", f"{where}: тип «{product.get('kind')}» — нужно {', '.join(schema.KINDS)}")
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,39}", code):
            plan.note("Не прошло проверки", f"{where}: код — только латиница, цифры и дефис")
            continue
        weight = _number(product.get("weight_g", ""))
        bot_name = (product.get("bot_name") or "").strip()
        bot_row = None
        if bot_name and _fold(bot_name) != _fold(_NOT_IN_CATALOG):
            bot_row = by_bot_name.get(_fold(bot_name))
            if bot_row is None:
                plan.note("Расхождения названий",
                          f"{where}: «Название в каталоге бота» = «{bot_name}», а в «Каталоге SKU» такого нет")
            else:
                linked.add(_fold(bot_name))

        item = {
            "sku_id": code,
            "kind": kind,
            # Название — то, под которым товар уже продаётся в боте: оно есть
            # в прошлых заказах, и по нему бот находит заварку и допродажу.
            "name": (bot_row or {}).get("name", "").strip() or (product.get("name") or "").strip(),
            "description": "",
            "net_weight_g": weight if kind in schema.PACKED else None,
            "raw_material": None,
            "gtin": None,
            "price": None,
            "on_sale": False,
            "vk_market_url": None,
            "recommend_with": [],
            "brew_guide": "",
            "video_url": None,
            "is_new": False,
            "synonyms": [],
            "sort_order": order * 10,
            "category": None,
            "active": _yes(product.get("active", ""), True),
        }
        sheet_price = _number(product.get("retail_price", ""))

        if kind in schema.PACKED:
            raw = _match_raw(product, fin_skus)
            if raw is None:
                plan.note("Нет пары в fin.sku",
                          f"{where} «{product.get('name')}» (артикул поставщика «{product.get('supplier_sku') or '—'}»): "
                          "сырья с таким артикулом или названием в финмодели нет — сырьё записано названием из «Товаров», проверить")
                item["raw_material"] = (product.get("name") or code).strip()
            else:
                matched_fin.add(raw["sku_name"])
                item["raw_material"] = raw["sku_name"]
                item["category"] = raw.get("category")
                fin_price = raw.get("retail_price") if weight is not None and raw.get("sale_pack_g") is not None \
                    and Decimal(str(raw["sale_pack_g"])) == weight else None
                if fin_price is not None:
                    item["price"] = int(Decimal(str(fin_price)))
            if weight is None or weight <= 0:
                plan.note("Не прошло проверки", f"{where}: «Вес, г» — «{product.get('weight_g')}», нужен вес больше нуля")
        else:
            bundle = fin_by_bundle.get(_fold(product.get("name", "")))
            item["category"] = "Наборы и сеты"
            if bundle is None:
                plan.note("Нет пары в fin.sku",
                          f"{where} «{product.get('name')}»: в fin.bundles нет набора с таким названием")
            else:
                matched_bundles.add(bundle["bundle"])
                if bundle.get("retail_price") is not None:
                    item["price"] = int(Decimal(str(bundle["retail_price"])))

        if item["price"] is None and sheet_price:
            item["price"] = int(sheet_price)
            plan.note("Цены", f"{code}: цены финмодели нет — взята из «Товаров»: {int(sheet_price)} ₽")
        elif item["price"] is not None:
            prices = {"финмодель": str(item["price"])}
            if sheet_price is not None:
                prices["«Товары»"] = str(int(sheet_price))
            if bot_row is not None:
                prices["бот сейчас"] = (bot_row.get("price") or "").strip() or "—"
            same = len(set(prices.values())) == 1
            plan.note("Цены", f"{code} «{item['name']}»: "
                      + ", ".join(f"{source} {value} ₽" for source, value in prices.items())
                      + (" — совпадают" if same else " → в базу цена финмодели"))
        if item["price"] is None:
            plan.note("Пусто", f"{code}: цены нет ни в финмодели, ни в «Товарах»")

        if bot_row is not None:
            _from_bot_row(plan, item, bot_row, code)
        elif kind in schema.PACKED or kind in schema.BUNDLES:
            plan.note("Не в «Каталоге SKU» — в продажу не включено", f"{code} «{item['name']}»")

        if not item["description"]:
            plan.note("Дозаполнить в Metabase", f"{code}: описание")
        if not item["brew_guide"] and kind in schema.PACKED:
            plan.note("Дозаполнить в Metabase", f"{code}: «Как заваривать»")
        if not item["gtin"]:
            plan.note("Дозаполнить в Metabase", f"{code}: GTIN — без него в продажу не включить")
        plan.note("Дозаполнить в Metabase",
                  f"{code}: поля этикетки (название на этикетке, вид продукции, хранение, срок годности, QR)")

        names_to_sku[_fold(item["name"])] = code
        if bot_row is not None:
            pending_recommend[code] = _list(bot_row.get("recommended", ""))
        plan.items.append(item)

    for fold, row in by_bot_name.items():
        if fold not in linked:
            plan.note("Расхождения названий",
                      f"«Каталог SKU», строка {row['_row']}: «{row.get('name')}» не связан ни с одним кодом во «Товарах» — не переносится")

    # «С чем советуем»: названия → коды.
    for item in plan.items:
        for name in pending_recommend.get(item["sku_id"], []):
            sku = names_to_sku.get(_fold(name))
            if sku is None:
                plan.note("«С чем советуем» не найдено", f"{item['sku_id']}: «{name}» — нет такого артикула")
            elif sku != item["sku_id"]:
                item["recommend_with"].append(sku)

    _plan_composition(plan, products, composition_tab)
    _report_fin_leftovers(plan, fin_skus, fin_bundles, matched_fin, matched_bundles)
    _report_settings(plan, settings_tab, inputs)
    return plan


def _from_bot_row(plan: Plan, item: dict, row: dict, code: str) -> None:
    """Тексты, ссылки и флаги — из «Каталога SKU»."""
    from app.modules.marking import codes

    where = f"«Каталог SKU», строка {row['_row']} ({code})"
    description, brewing_in_text = split_brewing(row.get("description", ""))
    brewing = " ".join((row.get("brewing") or "").split()) if row.get("brewing") else ""
    item["description"] = description[:1500]
    if len(description) > 1500:
        plan.note("Не прошло проверки", f"{where}: описание длиннее 1500 символов — обрезано, проверить")
    item["brew_guide"] = (row.get("brewing") or "").strip()[:1000]
    if brewing_in_text:
        in_text = _temperatures(brewing_in_text)
        in_column = _temperatures(brewing)
        if in_text and in_column and in_text != in_column:
            plan.note("Расхождения текстов",
                      f"{code}: в «Описании» вода {', '.join(f'{a}–{b} °C' for a, b in sorted(in_text))}, "
                      f"в «Как заваривать» {', '.join(f'{a}–{b} °C' for a, b in sorted(in_column))} → в базу столбец «Как заваривать», блок вынут из описания")
        else:
            plan.note("Расхождения текстов",
                      f"{code}: блок «Как заваривать» вынут из описания, в базу — столбец «Как заваривать»; сверьте граммы и время")
    raw_gtin = (row.get("gtin") or "").strip()
    if raw_gtin:
        gtin = codes.normalize_gtin(raw_gtin)
        if codes.gtin_is_valid(gtin):
            item["gtin"] = gtin
        else:
            plan.note("Не прошло проверки", f"{where}: GTIN «{raw_gtin}» с неверной контрольной цифрой — не перенесён")
    link = (row.get("link") or "").strip()
    if link:
        if _VK_MARKET.match(link):
            item["vk_market_url"] = link
        else:
            plan.note("Не прошло проверки", f"{where}: ссылка «{link}» — не Маркет ВК, не перенесена")
    video = (row.get("video") or "").strip()
    if video:
        if re.fullmatch(r"https://\S+", video):
            item["video_url"] = video
        else:
            plan.note("Не прошло проверки", f"{where}: видео «{video}» — не https, не перенесено")
    item["is_new"] = _yes(row.get("is_new", ""), False)
    item["synonyms"] = _list(row.get("synonyms", ""))
    wants_sale = _yes(row.get("in_stock", ""), True)
    if wants_sale and item["gtin"] and item["price"] and item["active"]:
        item["on_sale"] = True
    elif wants_sale:
        reason = "нет GTIN" if not item["gtin"] else "нет цены" if not item["price"] else "артикул не активен"
        plan.note("В таблице «в наличии», а в базе не в продаже",
                  f"{code} «{item['name']}»: {reason} — включить «Снять с продажи / вернуть» после дозаполнения")


def _plan_composition(plan: Plan, products: dict[str, dict], tab: references.Tab) -> None:
    """«Состав» → вложения наборов; пакетик другой фасовки — новый артикул."""
    created: dict[str, dict] = {}
    by_code = {item["sku_id"]: item for item in plan.items}
    totals: dict[str, Decimal] = {}
    pairs: dict[tuple[str, str], int] = {}
    for number, row in tab.rows:
        bundle = (row.get("product_code") or "").strip()
        component = (row.get("component_code") or "").strip()
        where = f"«{COMPOSITION_TAB}», строка {number}"
        if not bundle or not component or bundle == component:
            continue  # строка «пачка состоит из себя» — ничего не говорит
        bundle_item = by_code.get(bundle)
        component_item = by_code.get(component)
        if bundle_item is None or bundle_item["kind"] not in schema.BUNDLES:
            plan.note("Не прошло проверки", f"{where}: {bundle} — нет такого сета или набора во «Товарах»")
            continue
        if component_item is None or component_item["kind"] not in schema.PACKED:
            plan.note("Не прошло проверки", f"{where}: {component} — нет такой пачки или блина во «Товарах»")
            continue
        quantity = _number(row.get("quantity", ""))
        unit = (row.get("unit") or "").strip().casefold()
        if quantity is None or quantity <= 0:
            plan.note("Не прошло проверки", f"{where}: количество «{row.get('quantity')}»")
            continue
        if unit.startswith("шт"):
            item_sku, count, grams = component, int(quantity), (component_item["net_weight_g"] or 0) * quantity
        elif quantity == component_item["net_weight_g"]:
            item_sku, count, grams = component, 1, quantity
        else:
            grams_int = int(quantity)
            item_sku, count, grams = _component_code(component, grams_int), 1, quantity
            if item_sku not in by_code and item_sku not in created:
                created[item_sku] = {
                    **{key: None for key in ("gtin", "price", "vk_market_url", "video_url", "category")},
                    "sku_id": item_sku,
                    "kind": "пачка",
                    "name": f"{component_item['name']}, {grams_int} г (в наборе)",
                    "description": "",
                    "net_weight_g": quantity,
                    "raw_material": component_item["raw_material"],
                    "on_sale": False,
                    "recommend_with": [],
                    "brew_guide": component_item["brew_guide"],
                    "is_new": False,
                    "synonyms": [],
                    "sort_order": 900 + len(created),
                    "active": True,
                }
                plan.note("Новые артикулы пакетиков",
                          f"{item_sku} «{created[item_sku]['name']}» — из {component}, не в продаже; нужен свой GTIN и этикетка")
        pairs[(bundle, item_sku)] = pairs.get((bundle, item_sku), 0) + count
        totals[bundle] = totals.get(bundle, Decimal(0)) + Decimal(grams)
    plan.items.extend(created.values())
    plan.bundle_items = [
        {"bundle_sku": bundle, "item_sku": item, "quantity": quantity}
        for (bundle, item), quantity in pairs.items()
    ]
    for code, product in products.items():
        item = by_code.get(code)
        if item is None or item["kind"] not in schema.BUNDLES:
            continue
        declared = _number(product.get("weight_g", ""))
        if code not in totals:
            plan.note("Пусто", f"{code}: во «Составе» нет ни одного вложения")
        elif declared is not None and declared != totals[code]:
            plan.note("Не прошло проверки",
                      f"{code}: во «Товарах» вес {declared:g} г, а по «Составу» {totals[code]:g} г — в базе вес считается по составу")


def _report_fin_leftovers(plan, fin_skus, fin_bundles, matched_fin, matched_bundles) -> None:
    for sku in fin_skus:
        if sku["sku_name"] not in matched_fin:
            plan.note("Нет пары во «Товарах»", f"fin.sku «{sku['sku_name']}» — ни один код из «Товаров» на него не указывает")
    for bundle in fin_bundles:
        if bundle["bundle"] not in matched_bundles:
            plan.note("Нет пары во «Товарах»", f"fin.bundles «{bundle['bundle']}» — нет такого набора во «Товарах»")


def _report_settings(plan: Plan, tab: references.Tab, inputs: dict) -> None:
    """«Настройки» не импортируются — только видно, чем они отличаются от fin.inputs."""
    from app.core.config import free_delivery_threshold

    for number, row in tab.rows:
        key = (row.get("key") or "").strip()
        value = _number(row.get("value", ""))
        shown = (row.get("value") or "").strip()
        label = (row.get("name") or key).strip()
        if key in _BOT_SETTINGS:
            current = free_delivery_threshold()
            plan.note("«Настройки» против fin.inputs",
                      f"{key} ({label}): в таблице {shown}, в боте {_BOT_SETTINGS[key]} = {current if current is not None else 'выключен'}")
            continue
        fin_key = SETTINGS_TO_INPUTS.get(key, key)
        if fin_key not in inputs:
            plan.note("«Настройки» против fin.inputs", f"{key} ({label}): в таблице {shown}, в fin.inputs пары нет")
            continue
        current = inputs[fin_key]
        if isinstance(current, Decimal) and value is not None and current == value:
            continue
        plan.note("«Настройки» против fin.inputs",
                  f"{key} ({label}): в таблице {shown}, в fin.inputs {fin_key} = {current if current is not None else 'пусто'}")


async def _read_tabs() -> dict[str, list[list[str]]]:
    sheet_id = (settings.catalog_sheet_id or "").strip()
    if not sheet_id:
        raise RuntimeError("CATALOG_SHEET_ID не задан — таблицу не прочитать")
    names = {
        "catalog": settings.catalog_sheet_tab,
        "products": PRODUCTS_TAB,
        "composition": COMPOSITION_TAB,
        "settings": SETTINGS_TAB,
    }
    ranges = await google_sheets.client().batch_get(
        sheet_id, [google_sheets.a1_range(tab) for tab in names.values()]
    )
    return {key: sheet._canonical(rows) for key, rows in zip(names, ranges)}


async def _read_fin(session) -> tuple[list[dict], list[dict], dict]:
    if not await session.scalar(text("select to_regclass('fin.sku') is not null")):
        return [], [], {}
    skus = [dict(r) for r in (await session.execute(text(
        "select * from fin.sku where active order by sku_name"))).mappings()]
    bundles = [dict(r) for r in (await session.execute(text(
        "select * from fin.bundles where active order by bundle"))).mappings()]
    inputs = {
        row.key: (row.value if row.value is not None else row.value_text)
        for row in await session.execute(text("select key, value, value_text from fin.inputs_current"))
    }
    return skus, bundles, inputs


_INSERT = """
insert into shop.catalog (sku_id, kind, name, description, net_weight_g, raw_material, gtin, price,
                          on_sale, vk_market_url, brew_guide, video_url, is_new, synonyms, sort_order,
                          category, active, updated_by, change_note)
values (:sku_id, :kind, :name, :description, :net_weight_g, :raw_material, :gtin, :price,
        :on_sale, :vk_market_url, :brew_guide, :video_url, :is_new, :synonyms, :sort_order,
        :category, :active, :author, :note)
on conflict (sku_id) do nothing
returning sku_id
"""


async def run(*, apply: bool = False, tabs: dict | None = None) -> dict:
    """Отчёт импорта, а с `apply` — и запись новых артикулов."""
    tabs = tabs if tabs is not None else await _read_tabs()
    session_factory = get_session_factory()
    async with session_factory() as session:
        fin_skus, fin_bundles, inputs = await _read_fin(session)
    plan = build_plan(tabs, fin_skus, fin_bundles, inputs)
    if not fin_skus:
        plan.note("Вкладки не прочитаны", "схемы fin нет — цены и сырьё финмодели не сверены")
    result: dict = {
        "артикулов": len(plan.items),
        "вложений в наборах": len(plan.bundle_items),
        "в продажу": [item["sku_id"] for item in plan.items if item["on_sale"]],
        "отчёт": plan.report,
        "артикулы": [
            f"{item['sku_id']} {item['kind']} «{item['name']}» {item['price'] or '—'} ₽"
            + (" в продаже" if item["on_sale"] else "")
            for item in plan.items
        ],
    }
    if not apply:
        result["записано"] = "нет — пробный прогон; записать: ?apply=1"
        return result

    note = "импорт из Google Таблицы"
    async with session_factory() as session:
        before = await session.scalar(text("select coalesce(max(id), 0) from shop.catalog_changes"))
        inserted = []
        for item in plan.items:
            row = await session.execute(text(_INSERT), {**item, "author": AUTHOR, "note": note})
            if row.scalar() is not None:
                inserted.append(item["sku_id"])
        # «С чем советуем» — вторым проходом: триггер проверяет, что артикул
        # уже есть, а порядок строк в таблице произвольный.
        for item in plan.items:
            if item["sku_id"] in inserted and item["recommend_with"]:
                await session.execute(
                    text("update shop.catalog set recommend_with = :r, updated_by = :a, change_note = :n where sku_id = :s"),
                    {"r": item["recommend_with"], "a": AUTHOR, "n": note, "s": item["sku_id"]},
                )
        added_parts = 0
        for part in plan.bundle_items:
            if part["bundle_sku"] not in inserted:
                continue
            row = await session.execute(text(
                "insert into shop.bundle_items (bundle_sku, item_sku, quantity, updated_by, change_note) "
                "values (:bundle_sku, :item_sku, :quantity, :a, :n) on conflict (bundle_sku, item_sku) do nothing returning id"),
                {**part, "a": AUTHOR, "n": note})
            added_parts += row.scalar() is not None
        # Импорт — не правка владельца: в Ops ему хватит одной строки итога,
        # а не сообщения на каждый артикул.
        await session.execute(
            text("update shop.catalog_changes set notified_at = now() where id > :before and notified_at is null"),
            {"before": before},
        )
        await session.commit()
    skipped = [item["sku_id"] for item in plan.items if item["sku_id"] not in inserted]
    result["записано"] = {"артикулов": len(inserted), "вложений": added_parts,
                          "уже были в базе — не тронуты": skipped}
    if inserted and settings.telegram_ops_chat_id:
        from app.messages import manager as manager_messages

        await manager_messages.notify(
            manager_messages.CATALOG_CHANGES,
            f"<b>Каталог</b>\n📥 Импорт из таблицы: добавлено {len(inserted)} артикулов, "
            f"{added_parts} вложений в наборах. В продаже: {len(result['в продажу'])}.",
            chat_id=settings.telegram_ops_chat_id,
        )
    from app.modules.catalog import store

    await store.ensure_fresh(force=True)
    return result
