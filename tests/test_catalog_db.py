"""Каталог в базе (схема shop): проверки, журнал, кэш бота, пауза, импорт из таблицы.

Главное, что проверяется:
- база сама отклоняет плохие значения — форма Metabase и psql одинаково;
- каждое изменение попадает в журнал, роль Metabase журнал не пишет и
  ничего не удаляет;
- в режиме CATALOG_SOURCE=db бот не ходит в Google, правку видит за секунды,
  а пустой каталог — пауза для покупателя, не откат на таблицу;
- цена заказа не меняется вслед за каталогом;
- импорт из таблицы раскладывает «Товары» и «Состав» в артикулы и честно
  говорит, что не сошлось.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from app.core.config import settings
from app.modules.catalog import schema, service as catalog_service, sheet, sheet_import, store
from app.modules.stock import schema as stock_schema
from tests.test_fin_model import fin  # noqa: F401


def gtin(body: str) -> str:
    """Тринадцать цифр с верной контрольной: тело из двенадцати."""
    digits = [int(ch) for ch in body]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits))
    return body + str((10 - total % 10) % 10)


GTIN_A = gtin("460000000001")
GTIN_B = gtin("460000000002")


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgres://", "postgresql://")


@pytest.fixture
async def shop(clean, monkeypatch):
    """Пустой каталог в базе, бот читает его оттуда."""
    conn = await asyncpg.connect(_dsn())
    for name in ("metabase_fin", "metabase_ro"):
        await conn.execute(
            f"do $$ begin if not exists (select from pg_roles where rolname = '{name}') "
            f"then create role {name}; end if; end $$"
        )
    for statement in schema.DDL + stock_schema.DDL:
        await conn.execute(statement)
    await conn.execute(CLEAR)
    monkeypatch.setattr(settings, "catalog_source", "db")
    store.reset()
    try:
        yield conn
    finally:
        await conn.execute(CLEAR)
        await conn.close()
        store.reset()


CLEAR = ("truncate shop.stock_moves, shop.reservations, shop.lots, shop.bundle_items, shop.catalog, "
         "shop.catalog_changes restart identity cascade")


async def lot(conn, raw="Те Гуань Инь ЭКО (XFI0044)", qty=3000, unit="г", code=None, best_before=None, **fields):
    """Партия сырья на складе: без неё товар «не в наличии» для покупателя."""
    code = code or f"L-261101-T{await conn.fetchval('select count(*) from shop.lots') + 1}"
    row = {"lot_code": code, "raw_material": raw, "qty": Decimal(qty), "unit": unit, "best_before": best_before,
           "entered_by": "Никита", **fields}
    names = ", ".join(row)
    marks = ", ".join(f"${i}" for i in range(1, len(row) + 1))
    return await conn.fetchval(f"insert into shop.lots ({names}) values ({marks}) returning id", *row.values())


async def add(conn, sku_id="P-TGI-ECO-100", **fields):
    row = {
        "kind": "пачка", "name": "Те Гуань Инь", "net_weight_g": 100, "raw_material": "Те Гуань Инь ЭКО (XFI0044)",
        "gtin": GTIN_A, "price": 390, "on_sale": True, "updated_by": "Никита",
    }
    row.update(fields)
    columns = ", ".join(["sku_id", *row])
    placeholders = ", ".join(f"${i}" for i in range(1, len(row) + 2))
    await conn.execute(f"insert into shop.catalog ({columns}) values ({placeholders})", sku_id, *row.values())


# --- проверки базы ---------------------------------------------------------

@pytest.mark.parametrize("fields, message", [
    ({"gtin": GTIN_A[:-1] + str((int(GTIN_A[-1]) + 1) % 10)}, "контрольной цифрой"),
    ({"price": 0}, "больше нуля"),
    ({"name": ""}, "название обязательно"),
    ({"name": "Ч" * 81}, "не длиннее 80"),
    ({"kind": "коробка"}, "тип"),
    ({"raw_material": None}, "сырьё"),
    ({"gtin": None}, "без GTIN"),
    ({"vk_market_url": "https://ozon.ru/product/1"}, "Маркет ВК"),
    ({"video_url": "http://youtube.com/x"}, "https"),
    ({"brew_guide": "в" * 1001}, "1000"),
    ({"recommend_with": ["P-NOPE-100"]}, "С чем советуем"),
])
async def test_bad_values_are_rejected(shop, fields, message):
    with pytest.raises(asyncpg.RaiseError, match=message):
        await add(shop, **fields)
    assert await shop.fetchval("select count(*) from shop.catalog") == 0


async def test_recommend_must_not_point_to_itself(shop):
    await add(shop)
    with pytest.raises(asyncpg.RaiseError, match="С чем советуем"):
        await shop.execute("update shop.catalog set recommend_with = '{P-TGI-ECO-100}' where sku_id = 'P-TGI-ECO-100'")


async def test_gtin_and_name_are_unique(shop):
    await add(shop)
    with pytest.raises(asyncpg.UniqueViolationError):
        await add(shop, "P-DHP-100", name="Да Хун Пао")
    with pytest.raises(asyncpg.UniqueViolationError):
        await add(shop, "P-DHP-100", name="те гуань инь", gtin=GTIN_B)


async def test_sku_id_cannot_be_renamed(shop):
    await add(shop)
    with pytest.raises(asyncpg.RaiseError, match="нельзя переименовать"):
        await shop.execute("update shop.catalog set sku_id = 'P-OTHER-100'")


async def test_big_price_change_needs_confirmation(shop):
    await add(shop)
    with pytest.raises(asyncpg.RaiseError, match="30 %"):
        await shop.execute("update shop.catalog set price = 600")
    await shop.execute("update shop.catalog set price = 600, price_confirmed = true")
    # Подтверждение — на одну правку, а не навсегда.
    assert await shop.fetchval("select price_confirmed from shop.catalog") is False
    await shop.execute("update shop.catalog set price = 650")  # +8 % — без галочки


# --- журнал и права --------------------------------------------------------

async def test_every_change_is_journaled(shop):
    await add(shop)
    await shop.execute(
        "update shop.catalog set price = 420, description = 'Улун', updated_by = 'Илья', change_note = 'новый прайс'"
    )
    await shop.execute("update shop.catalog set on_sale = false")
    await shop.execute("update shop.catalog set on_sale = false")  # ничего не поменялось — ничего в журнал
    rows = await shop.fetch(
        "select action, field, old_value, new_value, changed_by, note from shop.catalog_changes order by id"
    )
    assert [r["action"] for r in rows] == ["добавлен", "изменён", "изменён", "изменён"]
    # Поля одной правки — в порядке ключей jsonb, а правки — по очереди.
    assert {r["field"] for r in rows[1:3]} == {"description", "price"} and rows[3]["field"] == "on_sale"
    (price,) = [r for r in rows if r["field"] == "price"]
    assert (price["old_value"], price["new_value"], price["changed_by"], price["note"]) == ("390", "420", "Илья", "новый прайс")
    # Автор не указан в этой правке — не «Илья» с прошлой, а роль базы.
    assert rows[3]["changed_by"] not in ("Илья", "Никита")
    assert await shop.fetchval("select change_note from shop.catalog") is None


async def test_bundle_composition_checks_and_journal(shop):
    await add(shop, "P-TGI-ECO-35", name="Те Гуань Инь, 35 г", net_weight_g=35, gtin=None, price=None, on_sale=False)
    await add(shop, "S-ENTRY", kind="набор", name="Набор «Точка входа»", net_weight_g=None, raw_material=None,
              gtin=GTIN_B, price=1490)
    with pytest.raises(asyncpg.RaiseError, match="не сет и не набор"):
        await shop.execute("insert into shop.bundle_items (bundle_sku, item_sku) values ('P-TGI-ECO-35', 'P-TGI-ECO-35')")
    await shop.execute("insert into shop.bundle_items (bundle_sku, item_sku, quantity) values ('S-ENTRY', 'P-TGI-ECO-35', 2)")
    await shop.execute("update shop.bundle_items set active = false")
    rows = await shop.fetch("select entity, action, field, old_value, new_value from shop.catalog_changes where entity = 'состав'")
    assert [tuple(r) for r in rows] == [
        ("состав", "добавлен", "P-TGI-ECO-35", None, "2"),
        ("состав", "изменён", "P-TGI-ECO-35", "2", "выключено"),
    ]


async def test_metabase_role_edits_but_never_deletes_or_forges_history(shop):
    await add(shop)
    async with shop.transaction():
        await shop.execute("set local role metabase_fin")
        await shop.execute("update shop.catalog set price = 400, updated_by = 'Никита' where sku_id = 'P-TGI-ECO-100'")
        await shop.execute(
            "insert into shop.catalog (sku_id, kind, name, net_weight_g, raw_material, price, updated_by) "
            "values ('P-DHP-100', 'пачка', 'Да Хун Пао', 100, 'Да Хун Пао (XFI0005)', 690, 'Никита')"
        )
    assert await shop.fetchval("select count(*) from shop.catalog_changes where field = 'price'") == 1
    for statement in (
        "delete from shop.catalog where sku_id = 'P-DHP-100'",
        "delete from shop.bundle_items",
        "insert into shop.catalog_changes (sku_id, entity, action) values ('x', 'артикул', 'добавлен')",
        "update shop.catalog_changes set new_value = '1'",
        "delete from shop.catalog_changes",
        "truncate shop.catalog",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with shop.transaction():
                await shop.execute("set local role metabase_fin")
                await shop.execute(statement)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with shop.transaction():
            await shop.execute("set local role metabase_ro")
            await shop.execute("update shop.catalog set price = 1")


# --- бот читает базу -------------------------------------------------------

async def test_bot_sees_catalog_in_its_usual_shape(shop):
    await add(shop, recommend_with=[], synonyms=["ТГИ"], brew_guide="85 °C", is_new=True,
              vk_market_url="https://vk.ru/market/product/x")
    await add(shop, "P-DHP-100", name="Да Хун Пао", gtin=GTIN_B, price=690, recommend_with=["P-TGI-ECO-100"])
    await add(shop, "P-TGI-ECO-35", name="Те Гуань Инь, 35 г (в наборе)", net_weight_g=35, gtin=None,
              price=None, on_sale=False)
    await add(shop, "P-OLD-100", name="Старый сорт", gtin=None, on_sale=False, active=False)
    await lot(shop)
    await store.ensure_fresh()
    items = {item["name"]: item for item in catalog_service.load_items()}
    # Пакетик без цены и архивный артикул бот не видит вовсе.
    assert set(items) == {"Те Гуань Инь", "Да Хун Пао"}
    tgi = items["Те Гуань Инь"]
    assert tgi["sku_id"] == "P-TGI-ECO-100" and tgi["kind"] == "pack"
    assert tgi["price"] == 390 and tgi["package_sizes"] == ["100 г"] and tgi["in_stock"]
    assert (tgi["brewing"], tgi["is_new"], tgi["synonyms"], tgi["link"]) == (
        "85 °C", True, ["ТГИ"], "https://vk.ru/market/product/x")
    assert items["Да Хун Пао"]["recommended"] == ["Те Гуань Инь"]
    assert catalog_service.gtin_for("Да Хун Пао").endswith(GTIN_B)
    assert catalog_service.upsell_for([{"name": "Да Хун Пао"}])["name"] == "Те Гуань Инь"


async def test_set_weight_is_the_sum_of_its_parts(shop):
    await add(shop, "P-TGI-ECO-35", name="ТГИ 35", net_weight_g=35, gtin=None, price=None, on_sale=False)
    await add(shop, "P-DHP-35", name="ДХП 35", net_weight_g=35, gtin=None, price=None, on_sale=False,
              raw_material="Да Хун Пао (XFI0005)")
    await add(shop, "S-ENTRY", kind="набор", name="Набор «Точка входа»", net_weight_g=None, raw_material=None, price=1490)
    await shop.execute("insert into shop.bundle_items (bundle_sku, item_sku, quantity) values "
                       "('S-ENTRY', 'P-TGI-ECO-35', 2), ('S-ENTRY', 'P-DHP-35', 2)")
    await store.ensure_fresh()
    (entry,) = catalog_service.load_items()
    assert entry["kind"] == "set" and entry["net_weight_g"] == 140 and entry["package_sizes"] == ["140 г"]


async def test_change_reaches_the_bot_within_seconds(shop, monkeypatch):
    await add(shop)
    clock = [1000.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    await store.ensure_fresh()
    assert catalog_service.load_items()[0]["price"] == 390
    await shop.execute("update shop.catalog set price = 420")
    clock[0] += 1
    await store.ensure_fresh()
    assert catalog_service.load_items()[0]["price"] == 390  # раньше пяти секунд базу не спрашиваем
    clock[0] += store._MEMORY_TTL_SECONDS
    await store.ensure_fresh()
    assert catalog_service.load_items()[0]["price"] == 420
    assert store._MEMORY_TTL_SECONDS <= 60


async def test_db_mode_never_calls_google(shop, monkeypatch):
    from app.core import google_sheets
    from app.modules.analytics import references

    def forbidden(*args, **kwargs):
        raise AssertionError("в режиме db бот не должен ходить в Google")

    monkeypatch.setattr(google_sheets, "client", forbidden)
    monkeypatch.setattr(sheet.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(settings, "sheets_auth_mode", "service_account")
    monkeypatch.setattr(settings, "catalog_sheet_id", "sheet")
    monkeypatch.setattr(settings, "references_sync_enabled", True)
    await add(shop)
    assert "CATALOG_SOURCE=db" in (await sheet.refresh(force=True))["skipped"]
    assert "CATALOG_SOURCE=db" in (await references.sync(force=True))["skipped"]
    await catalog_service.ensure_fresh()
    assert [item["name"] for item in catalog_service.load_items()] == ["Те Гуань Инь"]


async def test_empty_catalog_pauses_sales_instead_of_falling_back(shop, monkeypatch):
    from app.modules.dialog import telegram_client
    from app.modules.orders import conversation

    sent = []

    async def send_message(text_, chat_id=None, **_):
        sent.append((chat_id, text_))

    async def converse(*args, **kwargs):
        raise AssertionError("без каталога модель не спрашиваем")

    monkeypatch.setattr(telegram_client, "send_message", send_message)
    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100ops")
    await store.ensure_fresh()
    assert catalog_service.load_items() == [] and catalog_service.paused()

    reply = await conversation.handle_turn(5001, "Хочу Те Гуань Инь")
    assert reply == conversation.templates.catalog_paused()
    await conversation.handle_turn(5001, "Ау?")
    alerts = [message for chat, message in sent if chat == "-100ops"]
    assert len(alerts) == 1 and "Каталог в базе пуст" in alerts[0]


async def test_order_price_does_not_follow_the_catalog(shop):
    from app.modules.orders import repository
    from app.modules.orders.state import OrderDraft
    from app.modules.payment import yookassa_client

    await add(shop)
    await store.ensure_fresh()
    product = catalog_service.find_item("Те Гуань Инь")
    draft = OrderDraft(items=[{"name": product["name"], "quantity": 1, "price": product["price"]}],
                       items_total=product["price"])
    order = await repository.save_order(5002, draft, status="awaiting_payment")
    await shop.execute("update shop.catalog set price = 450")
    await store.ensure_fresh(force=True)
    assert catalog_service.find_item("Те Гуань Инь")["price"] == 450
    saved = await repository.by_id(order.id)
    assert saved.items[0]["price"] == 390
    assert yookassa_client.receipt_items(saved.items, 0, "")[0]["amount"]["value"] == "390.00"


async def test_ops_hears_about_price_and_sale_changes_once(shop, monkeypatch):
    from app.messages import manager as manager_messages

    sent = []

    async def notify(kind, text_, **kwargs):
        sent.append((kind, text_, kwargs.get("chat_id")))
        return True

    monkeypatch.setattr(manager_messages, "notify", notify)
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100ops")
    await add(shop)
    await shop.execute("update shop.catalog set price = 420, updated_by = 'Илья', change_note = 'прайс'")
    await shop.execute("update shop.catalog set description = 'просто текст'")
    await shop.execute("update shop.catalog set on_sale = false, updated_by = 'Никита'")
    result = await store.notify_changes()
    assert result == {"записей": 4, "в Ops": 3}
    ((kind, message, chat),) = sent
    assert (kind, chat) == (manager_messages.CATALOG_CHANGES, "-100ops")
    assert "Цена изменена: P-TGI-ECO-100 «Те Гуань Инь» 390 → 420 ₽ — Илья (прайс)" in message
    assert "снят с продажи — Никита" in message and "просто текст" not in message
    assert (await store.notify_changes())["записей"] == 0
    assert len(sent) == 1


# --- импорт из Google Таблицы ----------------------------------------------

TABS = {
    "catalog": [
        ["Название", "Цена", "Фасовки", "В наличии", "GTIN", "Ссылка", "Описание", "С чем советуем",
         "Как заваривать", "Видео", "Новинка"],
        ["Те Гуань Инь", "1501", "100 г", "да", "", "https://vk.ru/market/product/tgi",
         "Улун с ароматом сирени. Как заваривать: водой 80-85°C, проливом.", "Да Хун Пао",
         "Вода: 85–90 °C. Проливы 5–8 г на 150 мл.", "", "да"],
        ["Да Хун Пао", "1500", "100 г", "да", GTIN_B, "https://vk.ru/market/product/dhp",
         "Утёсный улун.", "Те Гуань Инь, Пуэр-призрак", "Вода: 95–100 °C.", "", "да"],
        ["Тестовый чай", "100", "100 г", "да", "123", "https://example.com", "x", "", "", "", ""],
    ],
    "products": [
        ["Код", "Название", "Тип", "Вес, г", "Розничная цена, ₽", "Артикул поставщика", "Роль", "Активен",
         "Комментарий", "Название в каталоге бота"],
        ["P-TGI-ECO-100", "Те Гуань Инь ЭКО", "пачка", "100", "390 ₽", "XFI0044", "", "да", "", "Те Гуань Инь"],
        ["P-DHP-100", "Да Хун Пао, сильная прожарка", "пачка", "100", "690 ₽", "XFI0005", "", "да", "", "Да Хун Пао"],
        ["P-GABA-100", "Габа Эко", "пачка", "100", "1 230 ₽", "IT-26", "", "да", "", "нет в каталоге"],
        ["B-DANCING-357", "Блин шу пуэр Танцующая Пара", "блин", "357", "1 900 ₽", "IT-101", "", "да", "", "нет в каталоге"],
        ["S-ENTRY", "Набор «Точка входа»", "набор", "140", "1 490 ₽", "", "", "да", "", "нет в каталоге"],
    ],
    "composition": [
        ["Код продукта", "Продукт", "Код сорта", "Сорт", "Количество", "Ед.", "Упаковка компонента"],
        ["P-TGI-ECO-100", "", "P-TGI-ECO-100", "", "100", "г", "дой-пак"],
        ["S-ENTRY", "", "P-TGI-ECO-100", "", "35", "г", "мини-пакет 35 г"],
        ["S-ENTRY", "", "P-DHP-100", "", "35", "г", "мини-пакет 35 г"],
    ],
    "settings": [
        ["Ключ", "Параметр", "Значение", "Ед.", "Действует с", "Источник", "Комментарий"],
        ["label_rub", "Этикетка 58×100", "1,565", "₽/шт", "01.10.2026", "", ""],
        ["marking_code_rub", "Код маркировки", "0,61", "₽/код", "01.10.2026", "", ""],
        ["usd_rub", "Курс доллара", "105", "₽/$", "01.10.2026", "", ""],
        ["mystery_key", "Что-то", "1", "", "01.10.2026", "", ""],
    ],
}
FIN_SKUS = [
    {"sku_name": "Те Гуань Инь ЭКО (XFI0044)", "category": "Светлый улун", "sale_pack_g": Decimal("100.0"),
     "retail_price": Decimal("390.00")},
    {"sku_name": "Да Хун Пао, сильная прожарка (XFI0005)", "category": "Тёмный улун", "sale_pack_g": Decimal("100.0"),
     "retail_price": Decimal("690.00")},
    {"sku_name": "Габа Вулканическая (IT-162)", "category": "Габа", "sale_pack_g": Decimal("100.0"),
     "retail_price": Decimal("1230.00")},
    {"sku_name": "Пуэр Танцующая Пара Баньна Фенг Мэнхай, 357гр (IT-101)", "category": "Шу пуэр, блин",
     "sale_pack_g": Decimal("357.0"), "retail_price": Decimal("1900.00")},
]
FIN_BUNDLES = [{"bundle": "Набор «Точка входа»", "retail_price": Decimal("1490.00")},
               {"bundle": "Сет «Шу и Шен»", "retail_price": Decimal("1790.00")}]
INPUTS = {"pack_label_cost": Decimal("0.31"), "code_price": Decimal("0.61"), "usd_rub": Decimal("87")}


def test_import_plan_from_sheet():
    plan = sheet_import.build_plan(TABS, FIN_SKUS, FIN_BUNDLES, INPUTS)
    items = {item["sku_id"]: item for item in plan.items}
    assert set(items) == {"P-TGI-ECO-100", "P-DHP-100", "P-GABA-100", "B-DANCING-357", "S-ENTRY",
                          "P-TGI-ECO-35", "P-DHP-35"}
    tgi = items["P-TGI-ECO-100"]
    # Название — то, под которым сорт уже продаётся; цена — финмодели.
    assert (tgi["name"], tgi["price"], tgi["raw_material"]) == ("Те Гуань Инь", 390, "Те Гуань Инь ЭКО (XFI0044)")
    assert tgi["description"] == "Улун с ароматом сирени." and tgi["brew_guide"].startswith("Вода: 85–90")
    # Без GTIN в продажу нельзя, с GTIN — можно.
    assert not tgi["on_sale"] and items["P-DHP-100"]["on_sale"]
    assert items["P-DHP-100"]["recommend_with"] == ["P-TGI-ECO-100"]
    assert tgi["recommend_with"] == ["P-DHP-100"]
    # Не в «Каталоге SKU» — не в продаже.
    assert not items["B-DANCING-357"]["on_sale"] and items["B-DANCING-357"]["kind"] == "блин"
    pouch = items["P-TGI-ECO-35"]
    assert (pouch["net_weight_g"], pouch["price"], pouch["on_sale"], pouch["raw_material"]) == (
        Decimal(35), None, False, "Те Гуань Инь ЭКО (XFI0044)")
    assert {(p["bundle_sku"], p["item_sku"], p["quantity"]) for p in plan.bundle_items} == {
        ("S-ENTRY", "P-TGI-ECO-35", 1), ("S-ENTRY", "P-DHP-35", 1)}
    assert items["S-ENTRY"]["price"] == 1490

    report = plan.report
    joined = "\n".join(line for lines in report.values() for line in lines)
    assert any("P-TGI-ECO-100" in line and "80–85 °C" in line and "85–90 °C" in line
               for line in report["Расхождения текстов"])
    assert any("P-GABA-100" in line for line in report["Нет пары в fin.sku"])
    assert any("Габа Вулканическая" in line for line in report["Нет пары во «Товарах»"])
    assert any("Сет «Шу и Шен»" in line for line in report["Нет пары во «Товарах»"])
    assert any("Пуэр-призрак" in line for line in report["«С чем советуем» не найдено"])
    assert any("Тестовый чай" in line for line in report["Расхождения названий"])
    assert any("бот сейчас 1501 ₽" in line for line in report["Цены"])
    settings_lines = report["«Настройки» против fin.inputs"]
    assert any("label_rub" in line and "pack_label_cost = 0.31" in line for line in settings_lines)
    assert any("usd_rub" in line and "105" in line and "87" in line for line in settings_lines)
    assert any("mystery_key" in line and "пары нет" in line for line in settings_lines)
    assert not any("marking_code_rub" in line for line in settings_lines)  # совпадает — не шумим
    assert "GTIN — без него в продажу не включить" in joined


async def test_import_writes_new_articles_only(shop, monkeypatch):
    async def no_fin(session):
        return FIN_SKUS, FIN_BUNDLES, INPUTS

    monkeypatch.setattr(sheet_import, "_read_fin", no_fin)
    dry = await sheet_import.run(tabs=TABS)
    assert dry["записано"].startswith("нет") and await shop.fetchval("select count(*) from shop.catalog") == 0

    result = await sheet_import.run(apply=True, tabs=TABS)
    assert result["записано"]["артикулов"] == 7 and result["записано"]["вложений"] == 2
    assert await shop.fetchval("select recommend_with from shop.catalog where sku_id = 'P-DHP-100'") == ["P-TGI-ECO-100"]
    # Записи импорта в Ops по одной не объявляются.
    assert await shop.fetchval("select count(*) from shop.catalog_changes where notified_at is null") == 0
    # В продаже — только Да Хун Пао (есть GTIN), и то лишь когда на складе есть его сырьё.
    assert [item["name"] for item in catalog_service.load_items() if item["in_stock"]] == []
    await lot(shop, "Да Хун Пао, сильная прожарка (XFI0005)")
    await store.ensure_fresh(force=True)
    assert [item["name"] for item in catalog_service.load_items() if item["in_stock"]] == ["Да Хун Пао"]

    await shop.execute("update shop.catalog set description = 'поправил Никита' where sku_id = 'P-DHP-100'")
    again = await sheet_import.run(apply=True, tabs=TABS)
    assert again["записано"]["артикулов"] == 0
    # Уже заведённые артикулы не «добавлены» второй раз и в журнале.
    assert await shop.fetchval("select count(*) from shop.catalog_changes where action = 'добавлен'") == 9
    assert await shop.fetchval("select description from shop.catalog where sku_id = 'P-DHP-100'") == "поправил Никита"


# --- финмодель берёт цену из каталога --------------------------------------


async def test_finmodel_price_is_the_catalog_price(fin):
    sku = await fin.fetchrow(
        "select sku_id, raw_material, price from shop.catalog where kind = 'пачка' order by sku_id limit 1")
    assert await fin.fetchval("select retail_price from fin.v_unit_cost where product = $1",
                              sku["raw_material"]) == sku["price"]
    await fin.execute("update shop.catalog set price = price + 10, updated_by = 'Никита' where sku_id = $1", sku["sku_id"])
    assert await fin.fetchval("select retail_price from fin.v_unit_cost where product = $1",
                              sku["raw_material"]) == sku["price"] + 10
    # В финмодели розничной цены больше нет — разойтись ей не с чем.
    assert not await fin.fetchval(
        "select count(*) from information_schema.columns where table_schema = 'fin' "
        "and table_name in ('sku', 'bundles') and column_name in ('retail_price', 'gtin', 'bot_name')")
    bundle = await fin.fetchrow("select bundle, sku_id from fin.bundles order by bundle limit 1")
    assert bundle["sku_id"] is not None
