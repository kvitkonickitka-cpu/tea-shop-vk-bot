"""Финмодель, миграция 006: налог без двойного учёта, факт ЮKassa, акты, безубыточность,
точка заказа, деньги на 8 недель, ворота пилота (пакет A: A1, A5, A6, A8–A13)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.test_fin_model import _order, fin, r2  # noqa: F401

TODAY = date.today()


async def _payment(conn, pid, amount, income, captured: datetime, *, order_id=None, method="bank_card", payout=None):
    await conn.execute(
        "insert into finance.yk_payments (id, status, amount, income_amount, payment_method, captured_at, order_id, payout_date)"
        " values ($1, 'succeeded', $2, $3, $4, $5, $6, $7)",
        pid, Decimal(amount), Decimal(income), method, captured, order_id, payout)


@pytest.fixture
async def registry(fin):
    await fin.execute("delete from finance.yk_payments; delete from finance.yk_refunds")
    yield fin
    await fin.execute("delete from finance.yk_payments; delete from finance.yk_refunds")


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


async def test_usn_reserve_is_reduced_by_paid_contributions(registry):
    """Было: 6 % со всей выручки и ещё взносы / 12. Стало: max(0; 6 % × доход − уплаченные взносы)."""
    fin = registry
    await _payment(fin, "a", 100000, 96500, _days_ago(3))
    await _payment(fin, "b", 50000, 48250, _days_ago(2))
    m0 = await fin.fetchrow("select * from fin.v_pnl_monthly where month_no = 0")
    # Доход КУДиР 150 000, взносы ещё не уплачены: резерв 9 000.
    assert m0["usn_income"] == Decimal("150000.00") and r2(m0["usn_reserve"]) == Decimal("9000.00")
    # Уплатили взносы 18 014 ₽: налог года max(0; 9 000 − 18 014) = 0 — резерв снимается целиком.
    await fin.execute("update fin.tax_calendar set status = 'оплачено', paid_amount = 18014, done_on = $1"
                      " where kind = 'взносы' and due_date = '2026-12-28'", TODAY - timedelta(days=1))
    m0 = await fin.fetchrow("select * from fin.v_pnl_monthly where month_no = 0")
    assert r2(m0["usn_reserve"]) == 0 and m0["contributions_paid"] == Decimal("18014.00")
    # Взносы — деньгами, не расходом в прибыли сверх налога.
    assert await fin.fetchval("select fin.usn_tax_ytd(current_date)") == 0
    # Через Новый год налог считается с начала каждого года заново.
    assert await fin.fetchval("select fin.usn_reserve_between('2026-12-15', '2027-01-15')") == 0


async def test_order_economics_takes_the_real_yookassa_fee_and_carrier_acts(registry, db):
    fin = registry
    order = await _order(db, 9301, [{"name": "Те Гуань Инь 100 г", "quantity": 2, "price": 390}], delivery=200, carrier=207.0)
    before = await fin.fetchrow("select * from fin.v_order_economics where order_id = $1", order)
    assert before["acquiring_source"] == "ставка способа" and before["carrier_cost_source"] == "котировка"
    # Реестр: ЮKassa удержала 33 ₽ (СБП дешевле карты) — эквайринг = факт + фискализация 1 %.
    await _payment(fin, "p", 980, 947, _days_ago(0), order_id=order, method="sbp")
    after = await fin.fetchrow("select * from fin.v_order_economics where order_id = $1", order)
    assert after["acquiring_source"] == "факт ЮKassa" and after["payment_method"] == "sbp"
    assert r2(after["acquiring"]) == Decimal("42.80")
    assert after["margin_before_cac"] - before["margin_before_cac"] == before["acquiring"] - after["acquiring"]
    # Акт СДЭКа по треку: доставка дороже котировки на 15 % и обратная доставка — отдельной статьёй.
    await fin.execute(
        "update orders set details = details || '{\"cdek_number\": \"10001\"}' where id = $1", order)
    await fin.execute(
        "insert into fin.carrier_acts (carrier, track, amount, kind, entered_by) values"
        " ('cdek', '10001', 230, 'доставка', 'Илья'), ('cdek', '10001', 150, 'возврат', 'Илья')")
    acted = await fin.fetchrow("select * from fin.v_order_economics where order_id = $1", order)
    assert acted["carrier_cost_source"] == "акт" and acted["carrier_cost"] == Decimal("230")
    assert acted["carrier_return_cost"] == Decimal("150")
    assert r2(after["margin_before_cac"] - acted["margin_before_cac"]) == Decimal("180.00")  # (230 − 200) + 150
    check = await fin.fetchrow("select * from fin.v_carrier_check where order_id = $1", order)
    assert r2(check["deviation"]) == Decimal("0.15") and check["over_limit"]


async def test_payment_rate_by_method_falls_back_to_cards(fin):
    assert await fin.fetchval("select fin.payment_rate_for('bank_card')") == Decimal("0.05148")
    # Ставки СБП нет — ставка карт, а не ноль.
    assert await fin.fetchval("select fin.payment_rate_for('sbp')") == Decimal("0.05148")
    await fin.execute("insert into fin.inputs (key, value, unit, entered_by) values ('acquiring_rate_sbp', 0.004, '%', 'Никита')")
    assert await fin.fetchval("select fin.payment_rate_for('sbp')") == Decimal("0.014880")


async def test_breakeven_switches_to_fact_after_ten_orders(fin, db):
    m1 = await fin.fetchrow("select * from fin.v_breakeven where month_no = 1")
    assert m1["orders_to_break_even"] == 27 and m1["margin_basis"].startswith("маржа «Набор «Точка входа»»")
    for n in range(10):
        await _order(db, 9400 + n, [{"name": "Набор «Точка входа»", "quantity": 1, "price": 1490}],
                     delivery=0, carrier=414.0, minutes=n)
    m1 = await fin.fetchrow("select * from fin.v_breakeven where month_no = 1")
    assert m1["fact_orders"] == 10 and m1["margin_basis"].startswith("факт")
    # Фактическая маржа набора с бесплатной доставкой 411 ₽ (из test_fin_model) вместо 788,79.
    assert r2(m1["margin_used"]) < Decimal("788.79") and m1["orders_to_break_even"] > 27


async def test_reorder_point_and_days_of_stock(fin, db):
    for n in range(7):
        await _order(db, 9500 + n, [{"name": "Те Гуань Инь 100 г", "quantity": 2, "price": 390}],
                     delivery=200, carrier=207.0, minutes=n)
    await fin.execute(
        "insert into fin.purchase_lots (date, sku_name, supplier, qty_units, price_per_unit, entered_by)"
        " values (current_date - 3, 'Те Гуань Инь ЭКО (XFI0044)', 'I-TEA', 3, 603.75, 'Никита');"
        " insert into fin.packing_runs (date, product, grams_in, packs_out, entered_by)"
        " values (current_date - 2, 'Те Гуань Инь ЭКО (XFI0044)', 2000, 20, 'Илья')")
    tgi = await fin.fetchrow("select * from fin.v_stock where product = 'Те Гуань Инь ЭКО (XFI0044)'")
    # Без сроков — точки заказа нет («нет данных»), а не «не пора».
    assert tgi["order_now"] is None and tgi["lead_days"] is None
    await fin.execute(
        "insert into fin.inputs (key, value, unit, entered_by) values"
        " ('supply_lead_days', 10, 'дней', 'Никита'), ('packing_marking_days', 2, 'дней', 'Никита'),"
        " ('safety_stock_days', 3, 'дней', 'Никита');"
        " insert into fin.supply_lead_times (scope, name, lead_days, entered_by) values ('поставщик', 'I-TEA', 20, 'Илья')")
    tgi = await fin.fetchrow("select * from fin.v_stock where product = 'Те Гуань Инь ЭКО (XFI0044)'")
    # Продано 14 пачек за 14 дней = 1 в день; точка заказа (20 + 2 + 3) × 1 = 25; на руках 6 → пора.
    assert tgi["lead_days"] == 20 and tgi["on_hand"] == 6
    assert r2(tgi["reorder_point"]) == Decimal("25.00") and tgi["order_now"] is True
    # Непродаваемый товар — в списке, с пометкой «нет в каталоге бота».
    stock = {row["product"]: row for row in await fin.fetch("select * from fin.v_stock")}
    assert any(row["not_in_bot"] for row in stock.values())


async def test_cash_forecast_eight_weeks_and_reserve_breach(registry):
    fin = registry
    rows = await fin.fetch("select * from fin.v_cash_forecast")
    assert len(rows) == 8 and rows[0]["week_start"] == TODAY
    # Без дневного бюджета рекламы прогноз помечен.
    assert rows[0]["ads_plan_missing"]
    # Остаток 58 500 − холды 15 995; ЮKassa в пути 1 930 приходит в первую неделю.
    await _payment(fin, "t", 2000, 1930, _days_ago(1))
    await fin.execute(
        "insert into fin.inputs (key, value, unit, entered_by) values ('ad_daily_budget', 1000, '₽/день', 'Никита')")
    await fin.execute(
        "insert into fin.inputs (key, value_text, unit, entered_by) values ('ad_campaign_start', $1, 'дата', 'Никита')",
        TODAY.isoformat())
    rows = await fin.fetch("select * from fin.v_cash_forecast")
    first = rows[0]
    assert first["payouts_in"] == Decimal("1930.00") and first["ads"] == 7000
    assert r2(first["opening"] + first["payouts_in"] - first["ads"] - first["taxes"] - first["bank_fee"]
              - first["services"] - first["planned_expenses"]) == r2(first["closing"])
    # 42 505 − 1 000/день — к резерву 30 000 подходим на третьей неделе.
    breach = first["reserve_breach_date"]
    assert breach is not None and TODAY + timedelta(days=7) < breach < TODAY + timedelta(days=21)
    assert any(row["below_reserve"] for row in rows)


async def test_pilot_gates_no_data_is_not_red(fin):
    gates = await fin.fetch("select * from fin.v_pilot_gates")
    assert [g["gate"] for g in gates] == ["Неделя 2", "Неделя 6", "Неделя 6", "М3", "М3", "М3"]
    assert {g["status"] for g in gates} == {"нет данных"}
    # Целевая доля повторов не задана — «нет данных», даже когда будут продажи.
    assert gates[3]["threshold"] is None


async def test_tax_calendar_is_filled(fin):
    rows = await fin.fetch("select due_date, kind, amount_hint from fin.tax_calendar order by due_date, event")
    assert rows[0]["due_date"] == date(2026, 12, 28) and rows[0]["kind"] == "взносы"
    # Взносы за неполный 2026 (с 08.09): 57 390 / 12 × (3 + 23/30).
    assert r2(rows[0]["amount_hint"]) == Decimal("18014.08")
    assert {r["due_date"] for r in rows} >= {date(2027, 4, 25), date(2027, 4, 28), date(2027, 7, 1), date(2027, 12, 28)}


async def test_metabase_reads_new_views(fin):
    """Новые представления читаются ролью дашбордов (функции fin — правами спрашивающего)."""
    await fin.execute("grant usage on schema analytics to metabase_ro; grant select on all tables in schema analytics to metabase_ro")
    async with fin.transaction():
        await fin.execute("set local role metabase_ro")
        for view in ("v_pnl_monthly", "v_cash_forecast", "v_pilot_gates", "v_breakeven", "v_stock", "v_cac",
                     "v_carrier_check_monthly", "v_kudir", "v_cash_status", "v_recent_entries"):
            await fin.fetch(f"select * from fin.{view}")


async def test_paid_cac_counts_only_clients_from_ads(fin, db):
    from app.modules.analytics import service as analytics

    await analytics.ensure_client(9601, ref="autumn")
    await analytics.ensure_client(9602)
    await _order(db, 9601, [{"name": "Набор «Точка входа»", "quantity": 1, "price": 1490}], delivery=0, carrier=414.0)
    await _order(db, 9602, [{"name": "Набор «Точка входа»", "quantity": 1, "price": 1490}], delivery=0, carrier=414.0,
                 minutes=1)
    await fin.execute("insert into fin.ad_spend (date, spend_own, entered_by) values (current_date, 600, 'Илья')")
    week = await fin.fetchrow("select * from fin.v_cac where period_type = 'неделя' and period_start = date_trunc('week', current_date)")
    # Общий CAC — на двух новых клиентов, платный — только на пришедшего по рекламе.
    assert week["new_clients"] == 2 and week["new_clients_ad"] == 1
    assert week["cac"] == 300 and week["cac_paid"] == 600
