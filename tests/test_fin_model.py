"""Финмодель в базе (схема fin): сверка с v1.5, экономика заказов, права формы.

Схема собирается теми же файлами, что и на бою (analytics/fin/migrations,
grants.sql), поверх тестовой базы бота. Выписки банка в тестовой базе нет —
вместо схемы finance кладётся заглушка с теми же столбцами, что читает fin.

Главное, что проверяется:
- при seed-вводных и пустом факте цифры те же, что в таблице v1.5;
- заказ считается по фактическим позициям, доставке и цене перевозчика, а
  позиция без себестоимости не даёт «маржу по известной части»;
- форма Metabase (роль metabase_fin) пишет только в таблицы ввода fin.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import asyncpg
import pytest

from app.core.config import settings

FIN = Path(__file__).resolve().parents[1] / "analytics" / "fin"

# Схему finance в тестовой базе создаёт сам бот (реестр ЮKassa), поэтому
# заглушка выписки — по таблицам, и убираются после теста только они.
FINANCE_STUB_TABLES = ("finance.fact_operations", "finance.articles", "finance.v_daily_balance_total",
                       "finance.v_pending_summary")
FINANCE_STUB = """
create schema if not exists finance;
create table finance.articles (article_id text primary key, grp text, section text, is_capex boolean default false,
                               article text);
create table finance.fact_operations (operation_date date, direction text, amount numeric, article_id text,
                                      operation_id text not null default md5(random()::text), split_no smallint default 1);
create table finance.v_daily_balance_total (day date, balance numeric);
create table finance.v_pending_summary (account_last4 text, checked_at timestamptz, check_failed boolean, holds bigint,
                                        holds_out numeric, holds_in numeric, holds_signed numeric);
insert into finance.articles values
    ('cff_owner_contribution', 'Собственники', 'CFF', false), ('cfo_out_services', 'Сервисы', 'CFO', false),
    ('cfo_in_yookassa', 'Выручка', 'CFO', false), ('cfo_out_ads_contractor', 'Маркетинг', 'CFO', false);
insert into finance.fact_operations values
    ('2026-09-10', 'in', 80000, 'cff_owner_contribution'), ('2026-10-08', 'out', 2000, 'cfo_out_services');
insert into finance.v_daily_balance_total values ('2026-10-14', 58985.51), ('2026-10-19', 58500.00);
insert into finance.v_pending_summary values ('4673', now(), false, 2, 15995, 0, -15995);
"""


# С миграции 012 цена, GTIN и название товара — из каталога бота (shop.catalog).
CATALOG_MIGRATION = "012_catalog_prices.sql"
CLEAR_SHOP = """do $$ begin
    if to_regclass('shop.catalog') is not null then
        truncate shop.bundle_items, shop.catalog, shop.catalog_changes restart identity cascade;
    end if;
end $$"""
# Каталог, каким его кладёт импорт: пачка и блин — на сырьё fin.sku с его
# фасовкой, сет и набор — под своим названием; цены — финмодели.
CATALOG_FROM_SEED = """
insert into shop.catalog (sku_id, kind, name, net_weight_g, raw_material, price, updated_by)
select 'P-T' || row_number() over (order by sku_name),
       case when supplier_unit ilike 'пресс%' then 'блин' else 'пачка' end,
       coalesce(bot_name, sku_name), sale_pack_g, sku_name, retail_price::int, 'тест'
from fin.sku where retail_price is not null;
insert into shop.catalog (sku_id, kind, name, price, updated_by)
select 'S-T' || row_number() over (order by bundle),
       case when pouch_type = '35g' then 'набор' else 'сет' end,
       coalesce(bot_name, bundle), retail_price::int, 'тест'
from fin.bundles where retail_price is not null;
"""


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgres://", "postgresql://")


@pytest.fixture
async def fin(db, clean):
    """Схема fin из файлов миграций; после теста — убрана вместе с заглушкой finance."""
    conn = await asyncpg.connect(_dsn())
    created_finance = False
    try:
        await conn.execute("drop schema if exists fin cascade")
        if not await conn.fetchval("select to_regclass('finance.fact_operations') is not null"):
            await conn.execute(FINANCE_STUB)
            created_finance = True
        for name in ("metabase_fin", "metabase_ro"):
            await conn.execute(
                f"do $$ begin if not exists (select from pg_roles where rolname = '{name}') "
                f"then create role {name}; end if; end $$"
            )
        await conn.execute(CLEAR_SHOP)
        seeded = False
        for migration in sorted((FIN / "migrations").glob("*.sql")):
            if not seeded and migration.name >= CATALOG_MIGRATION:
                seeded = True
                # Как на бою: до миграции 012 каталог импортирован из таблицы
                # с ценами финмодели — здесь его заменяет копия seed.
                await conn.execute(CATALOG_FROM_SEED)
            await conn.execute(migration.read_text(encoding="utf-8"))
        await conn.execute((FIN / "grants.sql").read_text(encoding="utf-8"))
        yield conn
    finally:
        await conn.execute(CLEAR_SHOP)
        await conn.execute("drop schema if exists fin cascade")
        if created_finance:
            await conn.execute(f"drop table if exists {', '.join(FINANCE_STUB_TABLES)} cascade")
        await conn.close()


def r2(value) -> Decimal:
    # Как Excel в v1.5: половина — вверх (85,625 → 85,63).
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- сверка с моделью v1.5 -------------------------------------------------------


async def test_seed_matches_model(fin):
    unit = {row["product"]: row for row in await fin.fetch("select * from fin.v_unit_cost")}
    # Лист 2, Q4: себестоимость пачки Те Гуань Инь ЭКО — 85,63 ₽.
    assert r2(unit["Те Гуань Инь ЭКО (XFI0044)"]["product_cost"]) == Decimal("85.63")
    # Лист 4, R5 и R7: маржа до CAC набора «Точка входа» и сета «Шу и Шен».
    # С миграцией 009 НДС начисляется и на плату за чек (ответ бухгалтера 21.10.2026):
    # нагрузка 5,368 % вместо 5,148 % в v1.5 — маржа чуть ниже таблицы (там 788,79 и 784,73).
    assert r2(unit["Набор «Точка входа»"]["margin_before_cac"]) == Decimal("785.07")
    assert r2(unit["Сет «Шу и Шен»"]["margin_before_cac"]) == Decimal("780.36")
    # Лист 3, L13: блин продаётся целым — без потерь и дой-пака.
    assert r2(unit["Пуэр Банжань Гушу >3лет, 357гр (IT-75)"]["product_cost"]) == Decimal("850.33")
    assert unit["Пуэр Банжань Гушу >3лет, 357гр (IT-75)"]["kind"] == "блин"
    # Лист 3, G4: порог цены при CAC 0.
    assert r2(unit["Те Гуань Инь ЭКО (XFI0044)"]["price_floor_cac0"]) == Decimal("353.90")
    assert {row["kind"] for row in unit.values()} == {"пачка", "блин", "сет", "набор"}

    # Лист 5, B37: заказов для безубыточности М1 — 27.
    m1 = await fin.fetchrow("select * from fin.v_breakeven where month_no = 1")
    assert m1["orders_to_break_even"] == 27
    assert r2(m1["fixed_costs"]) == Decimal("10575.50")

    # Лист 6, C19: известные расходы пилота — 173 174 ₽; с миграции 013 к ним добавлена
    # декларация о соответствии (33 000 ₽, в v1.5 её не было).
    budget = await fin.fetch("select * from fin.v_pilot_budget order by line_no")
    assert round(budget[0]["known_total"]) == 173174 + 33000
    assert {row["item"]: row["plan_amount"] for row in budget}["Декларация о соответствии"] == 33000
    assert {row["item"]: row["plan_amount"] for row in budget}["API тестов и консультаций до продаж"] is None

    assert await fin.fetchval("select fin.fmt_rub(173173.5975)") == "173 174 ₽"
    assert await fin.fetchval("select fin.fmt_pct(0.05148)") == "5,1 %"
    assert await fin.fetchval("select fin.fmt_rub(null)") == "нет данных"

    # Лист 1, строка 22: в v1.5 — 5,148 %; с 009 — (3,4 % + 1 % за чек) × 1,22 = 5,368 %.
    assert await fin.fetchval("select fin.payment_rate()") == Decimal("0.05368")


async def test_empty_fact_is_no_data_not_zero(fin):
    # Пилот начнётся 15.11: месяцы М1+ — без факта, статус лимита — «нет факта».
    future = await fin.fetch("select * from fin.v_pnl_monthly where period_start > current_date")
    assert future and all(row["revenue"] is None and row["operating_profit"] is None for row in future)
    status = await fin.fetchrow("select * from fin.v_cash_status")
    assert status["pilot_status"] == "НЕТ ФАКТА" and status["pilot_loss"] is None
    # Деньги — по выписке, снимок 14.10 сходится с ней до копейки.
    assert status["bank_balance"] == Decimal("58500.00") and status["snapshot_diff"] == 0
    assert status["holds_out"] == Decimal("15995")
    assert await fin.fetchval("select count(*) from fin.v_order_economics") == 0
    assert await fin.fetchval("select fact_margin_per_unit from fin.v_product_margin limit 1") is None


async def test_input_history_is_kept(fin):
    await fin.execute(
        "insert into fin.inputs (key, label, value, unit, valid_from, entered_by) "
        "values ('code_price', 'Код DataMatrix', 0.70, '₽/код', current_date, 'Илья')"
    )
    assert await fin.fetchval("select count(*) from fin.inputs where key = 'code_price'") == 2
    assert await fin.fetchval("select fin.inp('code_price')") == Decimal("0.70")
    # В «Последних записях» — название и единица, а не ключ.
    assert await fin.fetchval("select what from fin.v_recent_entries where form = 'Вводная'") == "Код DataMatrix: 0.7 ₽/код"
    # Прежнее значение не перезаписано — лежит рядом, с автором «импорт v1.5».
    assert await fin.fetchval("select value from fin.inputs where key = 'code_price' and entered_by = 'импорт v1.5'") == Decimal("0.61")
    with pytest.raises(asyncpg.CheckViolationError):
        await fin.execute(
            "insert into fin.inputs (key, value, entered_by) values ('code_price', 1, 'Вася')"
        )
    # Форма без значения не затирает вводную пустотой.
    with pytest.raises(asyncpg.CheckViolationError):
        await fin.execute("insert into fin.inputs (key, status, entered_by) values ('code_price', 'Изменено формой', 'Илья')")


async def test_purchase_lot_replaces_price_list(fin):
    # Партия со скидкой 10 % и без своей доставки: грамм дешевле, логистика — из вводных.
    await fin.execute(
        "insert into fin.purchase_lots (date, sku_name, supplier, qty_units, price_per_unit, discount, entered_by) "
        "values (current_date, 'Те Гуань Инь ЭКО (XFI0044)', 'I-TEA', 3, 603.75, 0.1, 'Никита')"
    )
    row = await fin.fetchrow("select * from fin.v_unit_cost where product = 'Те Гуань Инь ЭКО (XFI0044)'")
    assert r2(row["product_cost"]) == Decimal("79.59") and row["cost_source"] == "партии закупки"


# --- заказы бота -------------------------------------------------------------------


async def _order(db, peer_id, items, *, delivery, carrier, status="delivered", minutes=0):
    from app.modules.orders.models import Order

    items_total = sum(i["price"] * i["quantity"] for i in items)
    async with db() as session:
        order = Order(
            peer_id=peer_id, items=items, items_total=items_total, delivery_cost=delivery,
            total=items_total + delivery, status=status, payment_status="succeeded",
            paid_at=datetime.now(timezone.utc) - timedelta(minutes=60 - minutes),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=60 - minutes),
            details={"carrier_delivery_cost": carrier},
        )
        session.add(order)
        await session.commit()
        return order.id


async def test_orders_economics_pack_set_bundle_refund(fin, db):
    pack = await _order(db, 9101, [{"name": "Те Гуань Инь 100 г", "quantity": 2, "price": 390}],
                        delivery=200, carrier=207.0, minutes=1)
    bundle = await _order(db, 9102, [{"name": "Набор «Точка входа»", "quantity": 1, "price": 1490}],
                          delivery=0, carrier=414.0, minutes=2)
    refund = await _order(db, 9101, [{"name": "Сет «Шу и Шен»", "quantity": 1, "price": 1790}],
                          delivery=300, carrier=310.5, status="refunded", minutes=3)
    unknown = await _order(db, 9103, [{"name": "Улун неизвестный 100 г", "quantity": 1, "price": 500}],
                           delivery=0, carrier=0, minutes=4)

    e = {row["order_id"]: row for row in await fin.fetch("select * from fin.v_order_economics")}
    # Пачки: 2 × (чай 60,375 + логистика 14,4), упаковка 2 × 10,85 + коробка 100 + этикетка 0,31,
    # перевозчик 207 / 1,035, эквайринг 5,148 %, УСН 6 %, API нового покупателя 105.
    assert r2(e[pack]["cogs_tea"]) == Decimal("149.55")
    assert r2(e[pack]["packaging_and_codes"]) == Decimal("122.01")
    assert r2(e[pack]["carrier_cost"]) == Decimal("200.00") and r2(e[pack]["delivery_subsidy"]) == 0
    assert r2(e[pack]["margin_before_cac"]) == Decimal("292.03")
    assert e[pack]["is_first_order"] and e[pack]["two_plus_packs"]

    # Набор с бесплатной доставкой: маржа модели 785,07 + комиссия с доставки 22,74 − доставка 400.
    assert r2(e[bundle]["margin_before_cac"]) == Decimal("407.81")
    assert r2(e[bundle]["delivery_subsidy"]) == Decimal("400.00") and e[bundle]["free_delivery"]

    # Сет — повторный заказ того же клиента: API не считается; возврат помечен.
    assert not e[refund]["is_first_order"] and e[refund]["is_refunded"]
    assert r2(e[refund]["api_cost"]) == 0 and r2(e[refund]["margin_before_cac"]) == Decimal("873.99")

    # Позиция без справочника: себестоимость и маржа — «нет данных», а не маржа по известной части.
    assert e[unknown]["cogs_tea"] is None and e[unknown]["margin_before_cac"] is None
    assert e[unknown]["warning"]

    pnl = await fin.fetchrow("select * from fin.v_pnl_monthly where month_no = 0")
    assert pnl["orders"] == 4 and pnl["new_clients"] == 3
    assert r2(pnl["revenue"]) == Decimal("5060.00") and r2(pnl["returns"]) == Decimal("2090.00")
    # Прочие расходы М0 — по выписке (сервисы 08.10), а не из ручных расходов.
    assert r2(pnl["other_opex"]) == Decimal("2000.00")

    margins = {row["product"]: row for row in await fin.fetch("select * from fin.v_product_margin")}
    assert r2(margins["Набор «Точка входа»"]["fact_margin_per_unit"]) == Decimal("407.81")
    assert margins["Набор «Точка входа»"]["units_sold"] == 1


async def test_stock_and_reorder_signal(fin, db):
    await _order(db, 9201, [{"name": "Те Гуань Инь 100 г", "quantity": 2, "price": 390}], delivery=200, carrier=207.0)
    await _order(db, 9202, [{"name": "Сет «Шу и Шен»", "quantity": 1, "price": 1790}], delivery=0, carrier=0,
                 status="refunded")
    await fin.execute(
        """
        insert into fin.purchase_lots (date, sku_name, qty_units, price_per_unit, entered_by)
            values (current_date - 3, 'Те Гуань Инь ЭКО (XFI0044)', 3, 603.75, 'Никита');
        insert into fin.packing_runs (date, product, grams_in, packs_out, entered_by) values
            (current_date - 2, 'Те Гуань Инь ЭКО (XFI0044)', 2900, 29, 'Илья'),
            (current_date - 2, 'Набор «Точка входа»', null, 2, 'Илья'),
            (current_date - 2, 'Сет «Шу и Шен»', null, 1, 'Илья');
        """
    )
    stock = {row["product"]: row for row in await fin.fetch("select * from fin.v_stock")}
    tgi = stock["Те Гуань Инь ЭКО (XFI0044)"]
    # 3000 г куплено − 2900 расфасовано − 2 × 35 г в наборы.
    assert tgi["grams_left"] == 30 and tgi["on_hand"] == 27 and not tgi["reorder"]
    # Сет продан и возвращён: на полке 0 — «пора докупать».
    assert stock["Сет «Шу и Шен»"]["on_hand"] == 0 and stock["Сет «Шу и Шен»"]["reorder"]
    # Не фасовали — «нет данных», а не ноль.
    assert stock["Габа Вулканическая (IT-162)"]["on_hand"] is None


# --- права формы Metabase ------------------------------------------------------------


async def test_metabase_fin_writes_only_fin(fin):
    async with fin.transaction():
        await fin.execute("set local role metabase_fin")
        await fin.execute(
            "insert into fin.expenses (date, category, amount, description, entered_by) "
            "values (current_date, 'упаковка', 210, 'дой-пак', 'Илья')"
        )
        await fin.execute("insert into fin.inputs (key, value, entered_by) values ('code_price', 0.61, 'Никита')")
        assert await fin.fetchval("select count(*) from fin.v_order_economics") == 0
        assert await fin.fetchval("select count(*) from fin.v_unit_cost") == 16

    denied = [
        "update fin.inputs set value = 1 where key = 'code_price'",
        "delete from fin.inputs",
        "update fin.sku set price_per_unit_base = 1",
        "update shop.catalog_changes set new_value = '1'",
        "select count(*) from public.orders",
        "select count(*) from public.pii_vault",
        "insert into public.heartbeats (name, last_run_at, runs) values ('x', now(), 1)",
        "select count(*) from analytics.v_orders",
        "select count(*) from finance.fact_operations",
        "create table fin.hack (id int)",
    ]
    for statement in denied:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with fin.transaction():
                await fin.execute("set local role metabase_fin")
                await fin.execute(statement)

    async with fin.transaction():
        await fin.execute("set local role metabase_ro")
        assert await fin.fetchval("select count(*) from fin.v_pilot_budget") == 16
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await fin.execute("insert into fin.expenses (date, category, amount, entered_by) values (current_date, 'прочее', 1, 'Илья')")
