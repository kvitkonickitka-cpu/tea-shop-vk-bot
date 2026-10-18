"""Справочники из таблицы: разбор, правила пропуска строк, загрузка и маржа в analytics.

Sheets API подменён — настоящих запросов к таблице нет.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core import google_sheets
from app.core.config import settings
from app.modules.analytics import references as refs
from app.modules.analytics.models import RefCost, RefProduct, RefSetting, RefStockMove
from app.modules.catalog.models import CatalogSnapshot
from app.modules.orders.models import Order

NBSP = " "

PRODUCTS = [
    ["Код", "Название", "Тип", "Вес, г", "Розничная цена, ₽", "Артикул поставщика", "Роль", "Активен",
     "Комментарий", "Название в каталоге бота"],
    ["P-TGI-ECO-100", "Те Гуань Инь ЭКО", "пачка", "100", "390 ₽", "XFI0044", "Повторная покупка", "да", "",
     "Те Гуань Инь"],
    ["P-DHP-100", "Да Хун Пао, сильная прожарка", "пачка", "100", "690 ₽", "XFI0005", "", "да", "", "Да Хун Пао"],
    ["P-SHU15-100", "Шу пуэр Дворцовый 15 лет", "пачка", "100", f"1{NBSP}020 ₽", "IT-91", "", "да", "",
     "нет в каталоге"],
    ["S-SHU-SHEN", "Сет «Шу и Шен»", "сет", "200", f"1{NBSP}790 ₽", "", "", "да", "", "нет в каталоге"],
]
COMPOSITION = [
    ["Код продукта", "Продукт", "Код сорта", "Сорт", "Количество", "Ед.", "Упаковка компонента"],
    ["S-SHU-SHEN", "Сет «Шу и Шен»", "P-SHU15-100", "Шу пуэр", "100", "г", "дой-пак"],
    ["S-SHU-SHEN", "Сет «Шу и Шен»", "P-DHP-100", "Да Хун Пао", "100", "г", "дой-пак"],
]
COSTS = [
    ["Код", "Название", "Себестоимость единицы, ₽", "Действует с", "Источник", "Комментарий"],
    ["P-TGI-ECO-100", "Те Гуань Инь ЭКО", "91,95 ₽", "01.10.2026", "Тест: финмодель V14", "Чай + логистика"],
    ["P-DHP-100", "Да Хун Пао", "189,07 ₽", "01.10.2026", "Тест", ""],
    ["P-SHU15-100", "Шу пуэр", "283,57 ₽", "01.10.2026", "Тест", ""],
    ["S-SHU-SHEN", "Сет", "590,15 ₽", "01.10.2026", "Тест", ""],
]
SETTINGS_TAB = [
    ["Ключ", "Параметр", "Значение", "Ед.", "Действует с", "Источник", "Комментарий"],
    ["order_other_variable_rub", "Прочие переменные", "100", "₽/заказ", "01.10.2026", "V14", ""],
    ["label_rub", "Этикетка", "1,565", "₽/шт", "01.10.2026", "V14", ""],
    ["acquiring_cards_rate", "Эквайринг", "3,4%", "доля", "01.10.2026", "V14", ""],
    ["acquiring_vat_rate", "НДС на комиссию", "22,0%", "доля", "01.10.2026", "V14", ""],
    ["fiscalization_rate", "Чеки", "1,0%", "доля", "01.10.2026", "V14", ""],
    ["usn_reserve_rate", "УСН", "6,0%", "доля", "01.10.2026", "V14", ""],
    ["api_usd_per_consultation", "Claude API", "1", "$", "01.10.2026", "V14", ""],
    ["usd_rub", "Курс", "105", "₽/$", "01.10.2026", "V14", ""],
    ["bank_fee_rub_month", "Банк", "0", "₽/мес", "08.09.2026", "V14", ""],
    ["bank_fee_rub_month", "Банк", "1700", "₽/мес", "01.12.2026", "V14", "новой строкой"],
    ["packing_loss_rate", "Потери при фасовке", "", "доля", "", "V14", "Нет данных"],
]
MOVES = [
    ["Дата", "Операция", "Код сорта", "Сорт", "Количество", "Ед.", "Сумма партии, ₽", "Документ", "Комментарий"],
    ["01.10.2026", "приход", "P-TGI-ECO-100", "Те Гуань Инь ЭКО", "3000", "г", f"1{NBSP}811,25 ₽",
     "Заявка I-TEA 28.09 (тест)", "Тест"],
]


def sheet(**changes) -> list[list[list[str]]]:
    tabs = {"products": PRODUCTS, "composition": COMPOSITION, "costs": COSTS, "settings": SETTINGS_TAB,
            "moves": MOVES}
    tabs.update(changes)
    return [[list(r) for r in tabs[k]] for k in refs.TABS]


def tabs_of(ranges):
    return {kind: refs.read_tab(kind, google_sheets.pad_rows(rows)) for kind, rows in zip(refs.TABS, ranges)}


# --- разбор без базы ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("390 ₽", Decimal("390")), (f"1{NBSP}020 ₽", Decimal("1020")), ("91,95 ₽", Decimal("91.95")),
     ("1,565", Decimal("1.565")), ("0,61", Decimal("0.61")), ("1 811,25 ₽", Decimal("1811.25"))],
)
def test_numbers_in_russian_format(raw, expected):
    assert refs.number(raw) == expected


def test_percent_becomes_a_share():
    assert refs.number("3,4%", percent_ok=True) == Decimal("0.034")
    assert refs.number("22,0%", percent_ok=True) == Decimal("0.22")
    with pytest.raises(refs.BadValue, match="процент"):
        refs.number("3,4%")


@pytest.mark.parametrize("raw,expected", [("", "пусто"), ("-5", "отрицательное"), ("около 5", "не число")])
def test_bad_numbers(raw, expected):
    with pytest.raises(refs.BadValue, match=expected):
        refs.number(raw)


def test_dates():
    assert refs.day("01.10.2026") == date(2026, 10, 1)
    assert refs.day("2026-10-01") == date(2026, 10, 1)
    with pytest.raises(refs.BadValue):
        refs.day("1 октября")


def test_good_sheet_plans_everything():
    result = refs.plan(tabs_of(sheet()), {})
    assert result.problems == []
    products = result.plans["products"].upsert
    assert products[("P-TGI-ECO-100",)]["bot_name"] == "Те Гуань Инь"
    assert products[("P-SHU15-100",)]["bot_name"] is None  # «нет в каталоге»
    assert products[("P-SHU15-100",)]["retail_price"] == Decimal("1020")
    settings_ = result.plans["settings"].upsert
    assert settings_[("acquiring_cards_rate", date(2026, 10, 1))]["value"] == Decimal("0.034")
    assert settings_[("acquiring_cards_rate", date(2026, 10, 1))]["unit"] == "доля"
    # Одна настройка двумя строками с разными датами — обе остаются.
    assert ("bank_fee_rub_month", date(2026, 12, 1)) in settings_
    # Пустой параметр без даты («Нет данных») — не ошибка и не строка.
    assert not any(k[0] == "packing_loss_rate" for k in settings_)


def test_bad_rows_are_skipped_and_explained():
    costs = COSTS + [
        ["P-UNKNOWN", "Чего нет", "100 ₽", "01.10.2026", "", ""],
        ["P-DHP-100", "Да Хун Пао", "", "01.11.2026", "", ""],
        ["P-SHU15-100", "Шу пуэр", "-10", "01.11.2026", "", ""],
    ]
    result = refs.plan(tabs_of(sheet(costs=costs)), {})
    problems = "\n".join(result.problems)
    assert "«P-UNKNOWN» не найден на вкладке «Товары»" in problems
    assert "строка 7: пусто" in problems and "отрицательное число" in problems
    assert ("P-DHP-100", date(2026, 11, 1)) in result.plans["costs"].keep
    assert ("P-DHP-100", date(2026, 11, 1)) not in result.plans["costs"].upsert


def test_threefold_growth_is_refused():
    previous = {"products": {("P-TGI-ECO-100",): Decimal("390")},
                "costs": {("P-TGI-ECO-100", date(2026, 10, 1)): Decimal("91.95")}}
    products = [PRODUCTS[0], ["P-TGI-ECO-100", "Те Гуань Инь ЭКО", "пачка", "100", "3 900 ₽", "", "", "да", "", ""]]
    costs = COSTS[:1] + [["P-TGI-ECO-100", "x", "919,50 ₽", "01.11.2026", "", "лишний ноль"]]
    result = refs.plan(tabs_of(sheet(products=products, costs=costs, composition=COMPOSITION[:1],
                                     moves=MOVES[:1])), previous)
    problems = "\n".join(result.problems)
    assert "цена 3900 ₽ выросла больше чем в 3 раза (было 390 ₽)" in problems
    assert "себестоимость 919.50 ₽ выросла больше чем в 3 раза (было 91.95 ₽)" in problems
    assert ("P-TGI-ECO-100",) in result.plans["products"].keep


def test_missing_tab_is_left_alone():
    result = refs.plan(tabs_of(sheet(moves=[])), {})
    assert "moves" in result.skipped_tabs
    assert any("«Приходы и инвентаризация»: вкладка пустая" in p for p in result.problems)


# --- загрузка в базу -------------------------------------------------------------------


class FakeSheets:
    def __init__(self, ranges):
        self.ranges = ranges
        self.calls = 0

    async def batch_get(self, spreadsheet_id, ranges, **kwargs):
        self.calls += 1
        assert ranges == [google_sheets.a1_range(t) for t in refs.TABS.values()]
        return self.ranges


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(settings, "sheets_auth_mode", "service_account")
    monkeypatch.setattr(settings, "catalog_sheet_id", "sheet-id-for-tests")
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100")
    box = {"sent": [], "client": None}

    def serve(ranges):
        box["client"] = FakeSheets(ranges)
        monkeypatch.setattr(google_sheets, "_client", box["client"])

    async def notify(kind, text_, **kwargs):
        box["sent"].append((kind, text_, kwargs.get("chat_id")))
        return True

    monkeypatch.setattr("app.messages.manager.notify", notify)
    box["serve"] = serve
    return box


async def table(db, model):
    async with db() as session:
        return list((await session.execute(select(model))).scalars().all())


async def test_sync_loads_every_tab(clean, world):
    world["serve"](sheet())
    result = await refs.sync()
    assert result["проблем"] == 0 and world["sent"] == []
    assert len(await table(clean, RefProduct)) == 4
    assert {(c.code, c.unit_cost) for c in await table(clean, RefCost)} >= {("P-TGI-ECO-100", Decimal("91.95"))}
    assert len(await table(clean, RefSetting)) == 10
    [move] = await table(clean, RefStockMove)
    assert move.amount == Decimal("1811.25") and move.document == "Заявка I-TEA 28.09 (тест)"

    # Раз в 15 минут: следующий тик не читает таблицу снова.
    assert "skipped" in await refs.sync()
    assert world["client"].calls == 1
    later = datetime.now(timezone.utc) + timedelta(minutes=16)
    assert "применено" in await refs.sync(now=later)


async def test_bad_row_keeps_the_previous_value_and_tells_ops_once(clean, world):
    world["serve"](sheet())
    await refs.sync()
    broken = [COSTS[0], ["P-TGI-ECO-100", "x", "-1", "01.10.2026", "", ""]] + COSTS[2:]
    world["serve"](sheet(costs=broken))
    await refs.sync(force=True)
    await refs.sync(force=True)  # та же проблема — без второго сообщения
    cost = next(c for c in await table(clean, RefCost) if c.code == "P-TGI-ECO-100")
    assert cost.unit_cost == Decimal("91.95")
    assert len(world["sent"]) == 1
    kind, message, chat = world["sent"][0]
    assert kind == "references" and chat == "-100" and "отрицательное число" in message


async def test_row_removed_from_the_sheet_leaves_the_database(clean, world):
    world["serve"](sheet())
    await refs.sync()
    world["serve"](sheet(moves=MOVES[:1]))  # тестовый приход удалили, заголовок остался
    await refs.sync(force=True)
    assert await table(clean, RefStockMove) == []


async def test_public_mode_skips(monkeypatch):
    monkeypatch.setattr(settings, "sheets_auth_mode", "public")
    assert "skipped" in await refs.sync()


# --- маржа в analytics -------------------------------------------------------------------


async def view(db, name, order_by):
    async with db() as session:
        result = await session.execute(text(f"select * from analytics.{name} order by {order_by}"))
        return [dict(r._mapping) for r in result]


async def test_unit_economics_and_order_margin(clean, world):
    world["serve"](sheet())
    await refs.sync()
    paid_at = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    async with clean() as session:
        session.add(CatalogSnapshot(id=1, items=[
            {"name": "Те Гуань Инь", "price": 1500, "in_stock": True},
            {"name": "Да Хун Пао", "price": 1500, "in_stock": True},
        ]))
        session.add(Order(
            peer_id=9100, items=[{"name": "Те Гуань Инь", "quantity": 2, "price": 1500}],
            items_total=3000, delivery_cost=0, total=3000, delivery_method="ozon_pvz",
            status="confirmed", payment_status="succeeded", payment_id="pay-m1", paid_at=paid_at,
            details={"carrier_delivery_cost": 125.24}, created_at=paid_at,
        ))
        await session.commit()

    products = {p["code"]: p for p in await view(clean, "v_ref_products", "code")}
    tgi = products["P-TGI-ECO-100"]
    assert (tgi["retail_price"], tgi["unit_cost"], tgi["unit_margin"]) == (
        Decimal("390.00"), Decimal("91.95"), Decimal("298.05"))
    assert tgi["margin_pct"] == Decimal("76.4")
    assert tgi["in_bot_catalog"] is True and tgi["bot_price"] == Decimal("1500.00")
    assert tgi["bot_unit_margin"] == Decimal("1408.05")
    assert products["P-SHU15-100"]["in_bot_catalog"] is False
    assert products["S-SHU-SHEN"]["components"] == "Да Хун Пао, сильная прожарка 100 г · Шу пуэр Дворцовый 15 лет 100 г"

    [line] = await view(clean, "v_order_item_margin", "order_id")
    assert line["product_code"] == "P-TGI-ECO-100" and line["has_cost"] is True
    assert (line["cogs"], line["gross_margin"]) == (Decimal("183.90"), Decimal("2816.10"))

    [order] = await view(clean, "v_order_margin", "order_id")
    assert order["cogs"] == Decimal("183.90") and order["lines_without_cost"] == 0
    assert order["acquiring"] == Decimal("124.44")  # 3000 × 3,4 % × 1,22
    assert (order["fiscalization"], order["usn_reserve"]) == (Decimal("30.00"), Decimal("180.00"))
    assert (order["delivery_expense"], order["other_variable"], order["claude_api"]) == (
        Decimal("125.24"), Decimal("100.00"), Decimal("105.00"))
    assert order["contribution_margin"] == Decimal("2151.42")
    assert order["margin_pct"] == Decimal("71.7")

    settings_view = {s["key"]: s for s in await view(clean, "v_ref_settings", "key")}
    # Действует сегодня: из двух строк банка — та, что уже наступила.
    assert settings_view["bank_fee_rub_month"]["value"] == Decimal("0")
    costs_view = await view(clean, "v_ref_costs", "code")
    assert all(c["is_current"] for c in costs_view)


async def test_unlinked_item_is_flagged(clean, world):
    world["serve"](sheet())
    await refs.sync()
    now = datetime.now(timezone.utc)
    async with clean() as session:
        session.add(Order(peer_id=9101, items=[{"name": "Неизвестный чай", "quantity": 1, "price": 500}],
                          items_total=500, delivery_cost=0, total=500, delivery_method="ozon_pvz",
                          status="confirmed", payment_status="succeeded", payment_id="pay-m2",
                          paid_at=now, details={}, created_at=now))
        await session.commit()
    [order] = await view(clean, "v_order_margin", "order_id")
    assert order["lines_without_cost"] == 1 and order["cogs"] == Decimal("0.00")
