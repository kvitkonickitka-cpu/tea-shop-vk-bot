"""Цифры «Пульса» после ревизии 24.10 (миграция 019): одна сумма до старта, прогноз с планом продаж.

Проверяется на выписке «боя 20.10» (tests/test_fin_dashboards.py) и seed-вводных:
- «До старта заплатить», строка 1 «Требует внимания» и «Платежи впереди» до старта —
  одна сумма из fin.v_payments_plan, плановый расход формы не считается дважды;
- платёж в день ожидаемых вкладов, на который без них нет денег, — строка 24;
- линия «с планом продаж» совпадает с ручным расчётом;
- недели прогноза — с понедельника;
- сервисы до старта и за пилот: факт и план без двойного счёта.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tests.test_fin_dashboards import TODAY, bank  # noqa: F401
from tests.test_fin_model import fin  # noqa: F401


async def _input(conn, key, value=None, text=None):
    await conn.execute("insert into fin.inputs (key, value, value_text, entered_by) values ($1, $2, $3, 'Никита')",
                       key, value, text)


async def test_one_sum_before_start_in_three_places(bank):
    start = await bank.fetchval("select fin.inp_date('pilot_start_date')")
    plan = await bank.fetchval("select coalesce(sum(amount), 0) from fin.v_payments_plan where day < $1", start)
    ahead = await bank.fetchval(
        "select coalesce(sum(amount), 0) from fin.v_payments_ahead where not closed and day < $1", start)
    money = await bank.fetchval("select left_before_start from fin.v_money_now")
    assert plan > 0 and plan == ahead == money
    text = await bank.fetchval("select text from fin.v_attention where sort = 1")
    assert text and await bank.fetchval("select fin.fmt_rub($1)", money) in text and "осталось потратить до старта" in text

    # Плановый расход формы по статье: он и остаток статьи — не два платежа, сумма та же.
    await bank.execute(
        "insert into fin.expenses (date, amount, category, description, is_plan, paid_from, budget_item, entered_by) "
        "values ($1, 1000, 'упаковка', 'пакеты', true, 'счёт ИП', 'Упаковка и расходники (лимит)', 'Никита')",
        TODAY + timedelta(days=3))
    assert await bank.fetchval("select left_before_start from fin.v_money_now") == money


async def test_payment_on_capital_day_without_money_needs_attention(bank):
    # Закупка чая в тот же день, когда ждём вклады: без вкладов денег на неё нет.
    day = TODAY + timedelta(days=7)
    await _input(bank, "capital_expected_date", text=day.isoformat())
    await _input(bank, "tea_purchase_date", text=day.isoformat())
    await _input(bank, "pilot_start_date", text=(day + timedelta(days=30)).isoformat())
    await _input(bank, "vk_topup_date", text=(day + timedelta(days=14)).isoformat())
    # Сценарий проверяет нехватку на закупку, а не на платежи до неё.
    # Например, 1 ноября сервисы уже съедают свободные 2730,51 ₽ из bank.
    # Покрываем все более ранние платежи, оставляя перед закупкой 1 ₽ сверх резерва.
    earlier = await bank.fetchval(
        "select coalesce(sum(amount), 0) from fin.v_payments_plan where day < $1", day)
    reserve = await bank.fetchval("select fin.inp('min_cash_reserve')")
    await bank.execute("update finance.v_daily_balance_total set balance = $1", reserve + earlier + 1)
    assert await bank.fetchval(
        "select count(*) from fin.v_runway_daily where day < $1 and closing_no_capital < reserve", day) == 0
    money = await bank.fetchrow("select * from fin.v_money_now")
    assert money["capital_remaining"] > 0 and money["runout_no_capital"] == day
    text = await bank.fetchval("select text from fin.v_attention where sort = 24")
    assert text and "Первая закупка чая" in text and f"{day:%d.%m}" in text
    assert f"внесите вклады до {day - timedelta(days=1):%d.%m}" in text
    # Вклады раньше закупки — правило молчит.
    await _input(bank, "capital_expected_date", text=(day - timedelta(days=3)).isoformat())
    assert await bank.fetchval("select count(*) from fin.v_attention where sort = 24") == 0


async def test_forecast_with_sales_plan_matches_hand_calculation(bank):
    start = TODAY + timedelta(days=3)
    await _input(bank, "pilot_start_date", text=start.isoformat())
    assert await bank.fetchval("select count(*) from fin.v_runway_daily where closing_with_sales is not null") == 0
    for key, value in (("sales_plan_orders_m1", 30), ("sales_plan_orders_m2", 45), ("sales_plan_orders_m3", 60),
                       ("sales_mix_sets", 1), ("sales_mix_sets100", 0), ("sales_mix_packs", 0),
                       ("vk_topup_own", 60000), ("ad_plan_m1", 38000), ("ad_plan_m2", 22000), ("ad_plan_m3", 10000)):
        await _input(bank, key, value)
    # Ручной расчёт: маржа набора × заказы; реклама М1–М2 уже в пополнении ВК, М3 — сверх него.
    sets = await bank.fetchval("select avg(margin_before_cac) from fin.v_unit_cost where kind = 'набор' "
                               "and retail_price is not null")
    ends = [await bank.fetchval("select fin.pilot_month_start($1) - 1", n) for n in (2, 3, 4)]
    diff = {d: await bank.fetchval("select closing_with_sales - closing from fin.v_runway_daily where day = $1", d)
            for d in ends}
    assert round(diff[ends[0]], 2) == round(30 * sets, 2)
    assert round(diff[ends[1]], 2) == round((30 + 45) * sets, 2)
    assert round(diff[ends[2]], 2) == round((30 + 45 + 60) * sets - 10000, 2)
    # До старта линия совпадает с прогнозом без продаж.
    assert await bank.fetchval("select closing_with_sales - closing from fin.v_runway_daily where day = $1",
                               start - timedelta(days=1)) == 0


async def test_forecast_weeks_start_on_monday(bank):
    weeks = await bank.fetch("select week_no, week_start from fin.v_weekly_forecast order by week_no")
    assert weeks[0]["week_start"] == TODAY + timedelta(days=1)
    assert all(w["week_start"].isoweekday() == 1 for w in weeks[1:]) and len(weeks) == 8


async def test_services_before_start_and_in_pilot_without_double_count(bank):
    rows = {r["item"]: r for r in await bank.fetch(
        "select * from fin.v_pilot_budget_status where item in ('Сервисы до старта', 'Сервисы за пилот')")}
    infra = await bank.fetchval("select fin.inp('infra_monthly')")
    # Сервисы оплачивались с сентября: до старта (15.11) — сентябрь, октябрь и ноябрь.
    assert rows["Сервисы до старта"]["plan"] == infra * 3
    assert rows["Сервисы за пилот"]["plan"] == infra * await bank.fetchval("select fin.inp('pilot_months')")
    total = await bank.fetchval("select sum(amount) from fin.v_spend where budget_item in "
                                "('Сервисы до старта', 'Сервисы за пилот')")
    assert rows["Сервисы до старта"]["spent"] + rows["Сервисы за пилот"]["spent"] == total == Decimal("17812.49")


async def test_metabase_roles_run_without_jit(fin):
    # 020: JIT-компиляция съедала 3 с на запрос — у ролей Metabase он выключен.
    settings = await fin.fetch(
        "select r.rolname, s.setconfig from pg_db_role_setting s join pg_roles r on r.oid = s.setrole "
        "where r.rolname in ('metabase_ro', 'metabase_fin') and s.setdatabase = 0")
    assert {r["rolname"] for r in settings if "jit=off" in (r["setconfig"] or [])} == {"metabase_ro", "metabase_fin"}


async def test_low_margin_follows_threshold_input(fin):
    # Порог низкой маржи — вводная (с 24.10.2026 — 30 %): «Ниже порога» красит ровно то, что под ним.
    await _input(fin, "low_margin_threshold", Decimal("0.30"))
    cards = await fin.fetch("select margin_pct, low_margin from fin.v_product_card where margin_pct is not null")
    assert cards and all(c["low_margin"] == (c["margin_pct"] < Decimal("0.30")) for c in cards)
