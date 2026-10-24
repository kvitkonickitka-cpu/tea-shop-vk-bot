"""Финмодель, миграция 009: цифры для «Пульса» и «Служебного» (разбор дашбордов 20.10.2026, пакет D).

Выписка в тестах — как на бою 20.10.2026: 80 000 ₽ взноса, сентябрьские сервисы,
термопринтер 4 831 ₽ со счёта, два перевода себе на покупки 3 699 + 14 254 ₽ и
покупки с карты «мимо счёта» на 15 995 ₽ (они уже в seed fin.expenses).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.test_fin_model import fin, r2  # noqa: F401

TODAY = date.today()
PRINTER = "b2ef7c61-2ed8-007f-9544-e6e07ce16161"
ARTICLES = [
    ("cff_owner_contribution", "Собственники", "CFF", "Взнос собственника"),
    ("cfo_out_services", "Сервисы", "CFO", "Сервисы и подписки"),
    ("cfo_out_other", "Прочее", "CFO", "Прочие операционные расходы"),
    ("cfo_out_packaging", "Упаковка", "CFO", "Упаковка и расходники"),
    ("tech_internal_transfer", "Технические", "TECH", "Перевод между своими счетами"),
    ("tech_unclassified", "Технические", "TECH", "Не разобрано"),
]


async def _bank(conn, ops, balance, *, holds=0):
    """Выписка целиком: операции (дата, направление, сумма, статья[, id]) и остаток на последний день."""
    await conn.execute("delete from finance.fact_operations; delete from finance.v_daily_balance_total; "
                       "delete from finance.v_pending_summary")
    for article_id, grp, section, title in ARTICLES:
        await conn.execute(
            "insert into finance.articles (article_id, grp, section, article) values ($1, $2, $3, $4) "
            "on conflict (article_id) do update set article = excluded.article, section = excluded.section",
            article_id, grp, section, title)
    for op in ops:
        day, direction, amount, article_id, *rest = op
        await conn.execute(
            "insert into finance.fact_operations (operation_date, direction, amount, article_id, operation_id) "
            "values ($1, $2, $3, $4, coalesce($5, md5(random()::text)))",
            day, direction, Decimal(str(amount)), article_id, rest[0] if rest else None)
    await conn.execute("insert into finance.v_daily_balance_total values ($1, $2)", max(op[0] for op in ops), Decimal(balance))
    await conn.execute("insert into finance.v_pending_summary values ('4673', now(), false, 0, $1, 0, $2)",
                       Decimal(holds), -Decimal(holds))


# Бой на 20.10.2026 — датами того дня, чтобы месяцы сошлись.
OCTOBER = [
    (date(2026, 9, 10), "in", 80000, "cff_owner_contribution"),
    (date(2026, 9, 14), "out", 5000, "cfo_out_other"),
    (date(2026, 9, 25), "out", 4853.49, "cfo_out_services"),
    (date(2026, 10, 2), "out", 12959, "cfo_out_services"),
    (date(2026, 10, 6), "out", 4831, "cfo_out_other", PRINTER),
    (date(2026, 10, 16), "out", 473, "cfo_out_packaging"),
    (date(2026, 10, 20), "out", 3699, "tech_internal_transfer"),
    (date(2026, 10, 20), "out", 14254, "tech_internal_transfer"),
]


@pytest.fixture
async def bank(fin):
    await _bank(fin, OCTOBER, "32730.51")
    yield fin


async def test_free_flow_average_is_a_mean_not_a_sum(bank):
    """D1: было −28 116 (сумма двух месяцев), должно быть среднее по доступным месяцам."""
    months = {r["month"]: r for r in await bank.fetch("select * from fin.v_cash_monthly")}
    sep, oct_ = months[date(2026, 9, 1)], months[date(2026, 10, 1)]
    assert sep["free_flow"] == Decimal("-9853.49") and sep["free_flow_avg_3m"] == Decimal("-9853.49")
    assert oct_["free_flow"] == Decimal("-18263")
    assert r2(oct_["free_flow_avg_3m"]) == Decimal("-14058.25")
    # Переводы себе — не поток бизнеса, а отдельная строка.
    assert oct_["transfers"] == Decimal("-17953")


async def test_printer_is_an_investment(bank):
    """D3: термопринтер из «Прочих операционных» — во «Вложения», статья бюджета «Термопринтер»."""
    op = await bank.fetchrow("select * from fin.v_bank_ops where operation_id = $1", PRINTER)
    assert op["cash_class"] == "Вложения" and op["budget_item"] == "Термопринтер"
    oct_ = await bank.fetchrow("select * from fin.v_cash_monthly where month = '2026-10-01'")
    assert oct_["investing"] == Decimal("-4831") and oct_["operating"] == Decimal("-13432")
    ddc = {(r["cash_class"], r["item"]): r["amount"] for r in await bank.fetch("select * from fin.v_ddc where month = '2026-10-01'")}
    assert ddc[("Вложения", "Термопринтер")] == -4831
    assert ddc[("Переводы между своими счетами", "Перевод себе: на покупки")] == -17953
    # «Мимо счёта» весы — тоже вложения, упаковка — операционные.
    spend = {(r["source"], r["budget_item"]): r["cash_class"] for r in await bank.fetch("select * from fin.v_spend")}
    assert spend[("мимо счёта", "Весы")] == "Вложения"
    assert spend[("мимо счёта", "Упаковка и расходники (лимит)")] == "Операционные"
    # И из прибыли до пилота он ушёл: «прочие» М0 — сервисы и 5 000 ₽ без статьи.
    m0 = await bank.fetchval("select other_opex from fin.v_pnl_monthly where month_no = 0")
    assert m0 == Decimal("22812.49")


async def test_card_purchases_are_reconciled_not_counted_twice(bank):
    """D2: расходы = выписка без переводов себе + «мимо счёта»; не разнесено 17 953 − 15 995 = 1 958 ₽."""
    total = await bank.fetchval("select sum(amount) from fin.v_spend")
    # 5 000 + 4 853,49 + 12 959 + 4 831 + 473 по выписке и 15 995 с карты; переводы 17 953 — не расход.
    assert total == Decimal("44111.49")
    oct_ = await bank.fetchrow("select * from fin.v_card_reconcile where month = '2026-10-01'")
    assert (oct_["transferred"], oct_["recorded"], oct_["unreconciled"]) == (Decimal("17953"), Decimal("15995"), Decimal("1958"))
    status = await bank.fetchrow("select * from fin.v_card_reconcile_status")
    assert status["unreconciled"] == Decimal("1958")


async def test_unreconciled_card_transfer_shows_up_after_three_days(fin):
    ops = [(TODAY - timedelta(days=40), "in", 80000, "cff_owner_contribution"),
           (TODAY - timedelta(days=5), "out", 2000, "tech_internal_transfer"),
           (TODAY - timedelta(days=5), "out", 1000, "tech_internal_transfer", "own-1")]
    await _bank(fin, ops, "77000")
    await fin.execute("delete from fin.expenses")
    attention = await fin.fetch("select * from fin.v_attention")
    assert any("Не разнесены покупки с карты" in r["text"] and "3 000 ₽" in r["text"] for r in attention)
    # Перевод на свой счёт, а не на покупки, — разметкой; разница 2 000 ₽ остаётся.
    await fin.execute("insert into fin.bank_marks (operation_id, transfer_kind, entered_by) "
                      "values ('own-1', 'между своими счетами', 'Никита')")
    assert (await fin.fetchrow("select * from fin.v_card_reconcile_status"))["unreconciled"] == 2000
    # Покупку внесли — строки нет.
    await fin.execute("insert into fin.expenses (date, category, amount, paid_from, entered_by) "
                      "values (current_date, 'упаковка', 2000, 'мимо счёта', 'Никита')")
    assert not any("покупки с карты" in r["text"] for r in await fin.fetch("select * from fin.v_attention"))


async def test_free_cash_and_runway(bank):
    """D4: свободно = остаток − резерв (32 731 − 30 000); дата — из прогноза, пустая — не до конца горизонта."""
    money = await bank.fetchrow("select * from fin.v_money_now")
    assert money["free_cash"] == Decimal("2730.51") and money["reserve"] == 30000
    first = await bank.fetchrow("select * from fin.v_runway_daily order by day limit 1")
    assert first["day"] == TODAY + timedelta(days=1)
    # Оставшиеся 220 000 ₽ вкладов приходят одной суммой.
    assert await bank.fetchval("select sum(capital_in) from fin.v_runway_daily") == 220000
    breach = await bank.fetchval("select min(day) from fin.v_runway_daily where closing < reserve")
    assert money["runout_date"] == breach
    # Бюджет пилота тратится с прихода вкладов: до него прогноз ниже резерва не уходит.
    capital_day = await bank.fetchval("select min(day) from fin.v_runway_daily where capital_in > 0")
    # coalesce: когда дата вкладов уже прошла, они стоят на завтра, и дней до них нет —
    # пустая сумма значит «ничего не потрачено», а не провал (тест падал с 24.10.2026).
    assert await bank.fetchval("select coalesce(sum(budget_out), 0) from fin.v_runway_daily where day < $1",
                               capital_day) == 0
    assert breach is None or breach >= capital_day
    weeks = await bank.fetch("select * from fin.v_weekly_forecast")
    assert len(weeks) == 8 and weeks[0]["opening"] == Decimal("32730.51")
    chart = await bank.fetch("select * from fin.v_balance_chart where actual is not null")
    assert chart and chart[-1]["actual"] == Decimal("32730.51")


async def test_partner_capital_from_bank_and_form(bank):
    """D5: 80 000 из 300 000; форма говорит, чей вклад; новый взнос подхватывается без правок."""
    status = await bank.fetchrow("select * from fin.v_capital_status")
    assert (status["contributed"], status["plan"], status["remaining"]) == (80000, 300000, 220000)
    rows = {r["partner"]: r for r in await bank.fetch("select * from fin.v_partner_capital")}
    assert rows["Не указано, чей"]["contributed"] == 80000 and rows["Никита"]["plan"] == 150000
    await bank.execute("insert into fin.capital (date, partner, amount, kind, entered_by) "
                       "values ('2026-09-10', 'Никита', 80000, 'вклад', 'Никита')")
    rows = {r["partner"]: r for r in await bank.fetch("select * from fin.v_partner_capital")}
    assert "Не указано, чей" not in rows and rows["Никита"]["contributed"] == 80000
    await bank.execute("insert into finance.fact_operations (operation_date, direction, amount, article_id) "
                       "values ('2026-10-21', 'in', 220000, 'cff_owner_contribution')")
    assert await bank.fetchval("select contributed from fin.v_capital_status") == 300000


async def test_pilot_budget_plan_spent_left(bank):
    """D6: потрачено — выписка и «мимо счёта» по статьям; неизвестное — отдельной строкой."""
    rows = {r["item"]: r for r in await bank.fetch("select * from fin.v_pilot_budget_status")}
    assert (rows["Термопринтер"]["plan"], rows["Термопринтер"]["spent"], rows["Термопринтер"]["left_amount"]) == (4831, 4831, 0)
    assert rows["Весы"]["spent"] == 12296
    assert rows["Упаковка и расходники (лимит)"]["spent"] == Decimal("4172")  # 473 по выписке + 3 699 с карты
    assert rows["Сервисы за пилот"]["spent"] == Decimal("17812.49")
    assert rows["Не сопоставлено"]["spent"] == 5000 and rows["Не сопоставлено"]["plan"] is None


async def test_breakeven_from_ad_plan_and_scenarios(fin):
    """D7: М1–М3, ⌈(постоянные + реклама по плану) / маржа lead⌉ и сценарии 10 / 30 / 60 тыс."""
    plan = await fin.fetch("select * from fin.v_breakeven_plan order by month_no")
    assert [r["month_label"] for r in plan] == ["М1", "М2", "М3"]
    m1 = plan[0]
    fixed = await fin.fetchval("select fin.fixed_month(fin.pilot_month_start(1)) + fin.contractor_monthly()")
    assert m1["ads_plan"] == 10000 and m1["fixed_costs"] == fixed
    assert m1["orders_needed"] == math.ceil((fixed + 10000) / m1["lead_margin"])
    assert all(r["orders_needed"] is not None for r in plan)
    sc = {r["ads"]: r["orders_needed"] for r in await fin.fetch("select * from fin.v_breakeven_scenarios where month_no = 1")}
    assert set(sc) == {10000, 30000, 60000} and sc[10000] == m1["orders_needed"]
    assert sc[60000] == math.ceil((fixed + 60000) / m1["lead_margin"])


async def test_one_margin_names_and_low_margin_flag(fin):
    """D8–D10: маржа одна — fin.v_unit_cost; имя без кода поставщика; низкая маржа — ниже порога."""
    cards = {r["product"]: r for r in await fin.fetch("select * from fin.v_product_card")}
    tgi = cards["Те Гуань Инь ЭКО (XFI0044)"]
    assert (tgi["name"], tgi["supplier_code"], tgi["kind_group"]) == ("Те Гуань Инь ЭКО", "XFI0044", "Пачки")
    assert tgi["low_margin"] and tgi["sold_by_bot"]
    assert cards["Дянь Хунь фермерский (XFI0095)"]["name"] == "Дянь Хун фермерский"
    lead = cards["Набор «Точка входа»"]
    assert lead["name"] == "Набор «Точка входа»" and not lead["low_margin"] and lead["supplier_code"] is None
    unit = await fin.fetchval("select margin_before_cac from fin.v_unit_cost where product = 'Набор «Точка входа»'")
    assert lead["margin_before_cac"] == unit
    # Водопад сходится к той же марже у каждого товара.
    sums = await fin.fetch("select w.product, sum(w.amount) as s, u.margin_before_cac as m from fin.v_unit_waterfall w "
                           "join fin.v_unit_cost u using (product) group by w.product, u.margin_before_cac")
    assert sums and all(abs(r["s"] - r["m"]) < Decimal("0.0001") for r in sums)


async def test_pnl_report_and_pilot_status(fin):
    """D14 / D15: P&L статьями, М0–М3; до старта — «не начался» и сколько дней."""
    rows = await fin.fetch("select * from fin.v_pnl_report order by sort")
    assert rows[0]["item"] == "Заказы, шт." and rows[-1]["item"] == "ОПЕРАЦИОННАЯ ПРИБЫЛЬ" and rows[-1]["is_total"]
    m0 = await fin.fetchval("select operating_profit from fin.v_pnl_monthly where month_no = 0")
    assert rows[-1]["m0"] == m0
    status = await fin.fetchrow("select * from fin.v_pilot_status")
    assert status["start_date"] == date(2026, 11, 15)
    assert status["days_to_start"] == (date(2026, 11, 15) - TODAY).days


async def test_attention_says_all_is_well_when_nothing_is_wrong(fin):
    # Денег хватает, партия внесена, переводов нет — проверкам нечего сказать.
    await _bank(fin, [(TODAY - timedelta(days=30), "in", 900000, "cff_owner_contribution", "capital-1")], "900000")
    await fin.execute("insert into fin.bank_marks (operation_id, partner, entered_by) values ('capital-1', 'Илья', 'Илья')")
    await fin.execute("insert into shop.lots (lot_code, raw_material, qty, price_per_unit, entered_by) "
                      "select 'L-261101-T1', sku_name, 1000, 1000, 'Никита' from fin.sku limit 1")
    # Настоящая декларация вместо тестовой из миграции 015.
    await fin.execute("insert into fin.inputs (key, value_text, entered_by) values "
                      "('chz_declaration_number', 'ЕАЭС N RU Д-CN.РА01.В.12345/26', 'Никита')")
    rows = await fin.fetch("select * from fin.v_attention")
    started = await fin.fetchval("select started from fin.v_pilot_status")
    if not started:
        assert [(r["level"], r["text"]) for r in rows] == [("Всё в порядке", "Проверки ничего не нашли")]


async def test_new_views_are_readable_by_dashboards_and_marks_by_forms(bank):
    await bank.execute("grant usage on schema analytics to metabase_ro; grant select on all tables in schema analytics to metabase_ro")
    async with bank.transaction():
        await bank.execute("set local role metabase_ro")
        for view in ("v_bank_ops", "v_spend", "v_card_reconcile", "v_card_reconcile_status", "v_cash_monthly", "v_ddc",
                     "v_partner_capital", "v_capital_status", "v_pilot_budget_status", "v_runway_daily",
                     "v_balance_chart", "v_money_now", "v_weekly_forecast", "v_pilot_status", "v_breakeven_plan",
                     "v_breakeven_scenarios", "v_product_card", "v_unit_waterfall", "v_claude_daily",
                     "v_claude_status", "v_pnl_report", "v_kpi_targets", "v_attention", "v_yk_payouts",
                     "v_yk_receipts_missing"):
            await bank.fetch(f"select * from fin.{view}")
    async with bank.transaction():
        await bank.execute("set local role metabase_fin")
        await bank.execute("insert into fin.bank_marks (operation_id, budget_item, entered_by) "
                           "values ('x', 'Весы', 'Илья')")
        with pytest.raises(Exception):
            await bank.execute("delete from fin.budget_article_map")


async def test_payment_without_receipt_needs_attention(fin):
    await fin.execute("delete from finance.yk_payments")
    try:
        await fin.execute(
            "insert into finance.yk_payments (id, status, amount, income_amount, payment_method, captured_at, receipt_registration)"
            " values ('p-no-receipt', 'succeeded', 1490, 1430, 'bank_card', now() - interval '2 hours', 'pending')")
        texts = [r["text"] for r in await fin.fetch("select * from fin.v_attention")]
        assert "Чек не зарегистрирован у ЮKassa: 1 оплат(ы)" in texts
    finally:
        await fin.execute("delete from finance.yk_payments")


# --- 010: доработки по ревью 21.10.2026 ---------------------------------------------------


async def test_refunded_payment_is_not_in_transit(bank):
    """«Придёт 219 992 ₽»: тестовый платёж 217 ₽ (нетто 208,75) с возвратом давал −8,25 ₽ в прогнозе."""
    await bank.execute("delete from finance.yk_refunds; delete from finance.yk_payments")
    try:
        await bank.execute(
            "insert into finance.yk_payments (id, status, amount, income_amount, payment_method, captured_at) values "
            "('p-refunded', 'succeeded', 217, 208.75, 'bank_card', now() - interval '25 days'), "
            "('p-live', 'succeeded', 1490, 1430.40, 'bank_card', now() - interval '1 hour')")
        await bank.execute("insert into finance.yk_refunds (id, payment_id, status, amount, created_at) "
                           "values ('r-1', 'p-refunded', 'succeeded', 217, now() - interval '25 days')")
        assert await bank.fetchval("select sum(payouts_in) from fin.v_runway_daily") == Decimal("1430.40")
        week = await bank.fetchrow("select * from fin.v_weekly_forecast where week_no = 1")
        assert week["inflow"] == Decimal("220000") + Decimal("1430.40")
    finally:
        await bank.execute("delete from finance.yk_refunds; delete from finance.yk_payments")


async def test_tea_and_vk_on_their_dates_and_forecast_without_capital(bank):
    tea_day = await bank.fetchval("select greatest(fin.inp_date('tea_purchase_date'), current_date + 1)")
    vk_day = await bank.fetchval("select greatest(fin.inp_date('vk_topup_date'), current_date + 1)")
    tea = await bank.fetchval("select sum(left_amount) from fin.v_pilot_budget_status "
                              "where item in ('Первая закупка чая', 'Доставка основной партии')")
    on_tea_day = await bank.fetchval("select budget_out from fin.v_runway_daily where day = $1", tea_day)
    assert on_tea_day >= tea
    assert await bank.fetchval("select ads_out from fin.v_runway_daily where day = $1", vk_day) == 60000
    assert await bank.fetchval("select count(*) from fin.v_runway_daily where ads_out > 0") == 1
    money = await bank.fetchrow("select * from fin.v_money_now")
    # Без вкладов 2 731 ₽ сверх резерва не хватит и на закупку чая.
    assert money["runout_no_capital"] is not None and money["capital_remaining"] == 220000
    chart = await bank.fetch("select day, count(*) over (partition by day) as n from fin.v_balance_chart")
    assert all(r["n"] == 1 for r in chart)


async def test_contribution_is_attributed_by_marking_the_bank_operation(fin):
    """Ответ владельца: 80 000 ₽ — Никиты, разметкой операции, без второй записи о поступлении."""
    await _bank(fin, [(date(2026, 9, 10), "in", 80000, "cff_owner_contribution", "capital-80"),
                      (date(2026, 9, 21), "out", 1000, "tech_internal_transfer", "draw-1")], "79000")
    await fin.execute("insert into fin.bank_marks (operation_id, budget_item, entered_by) values ('capital-80', 'Весы', 'Илья')")
    await fin.execute("insert into fin.bank_marks (operation_id, partner, entered_by) values ('capital-80', 'Никита', 'Никита')")
    rows = {r["partner"]: r for r in await fin.fetch("select * from fin.v_partner_capital")}
    assert rows["Никита"]["contributed"] == 80000 and "Не указано, чей" not in rows
    assert await fin.fetchval("select contributed from fin.v_capital_status") == 80000
    # Поле «Чей вклад» не стирает статью, поставленную раньше.
    assert await fin.fetchval("select budget_item from fin.v_bank_ops where operation_id = 'capital-80'") == "Весы"
    # Перевод себе без разметки — «на покупки» по умолчанию и строка «проверьте».
    texts = [r["text"] for r in await fin.fetch("select * from fin.v_attention")]
    assert any(t.startswith("Переводы себе без разметки: 1 на 1 000 ₽") for t in texts)
    await fin.execute("insert into fin.bank_marks (operation_id, transfer_kind, entered_by) "
                      "values ('draw-1', 'вывод владельцем', 'Никита')")
    op = await fin.fetchrow("select * from fin.v_bank_ops where operation_id = 'draw-1'")
    assert (op["transfer_kind"], op["cash_class"], op["transfer_kind_default"]) == ("вывод владельцем", "Финансирование", False)
    assert (await fin.fetchrow("select * from fin.v_card_reconcile_status"))["transferred"] == 0


async def test_any_old_uncovered_transfer_is_overdue(fin):
    """Непокрытый перевод старше N дней — даже если последний перевод свежий."""
    await _bank(fin, [(TODAY - timedelta(days=30), "out", 1000, "tech_internal_transfer"),
                      (TODAY, "out", 17953, "tech_internal_transfer")], "50000")
    await fin.execute("delete from fin.expenses")
    assert (await fin.fetchrow("select * from fin.v_card_reconcile_status"))["overdue"]
    await fin.execute("insert into fin.expenses (date, category, amount, paid_from, entered_by) "
                      "values (current_date, 'упаковка', 1000, 'мимо счёта', 'Никита')")
    assert not (await fin.fetchrow("select * from fin.v_card_reconcile_status"))["overdue"]


async def test_unmatched_spend_and_budget_before_and_in_pilot(bank):
    texts = [r["text"] for r in await bank.fetch("select * from fin.v_attention")]
    assert "Разметить 1 операц. на 5 000 ₽: статья бюджета не определена" in texts
    services = await bank.fetchrow("select * from fin.v_pilot_budget_status where item = 'Сервисы за пилот'")
    assert services["spent_before_start"] == services["spent"] and services["spent_in_pilot"] == 0


async def test_inputs_display(fin):
    rows = {r["key"]: r for r in await fin.fetch("select * from fin.v_inputs_display")}
    assert rows["capital_plan"]["kind"] == "решение"
    assert rows["ip_registration_date"]["theme"] == "3. Налоги" and rows["ip_registration_date"]["value_shown"] == "08.09.2026"
    assert rows["ip_fixed_contrib_2026"]["comment"].startswith("Решение · Расчёт 21.10")
    assert rows["acquiring_rate_bank_card"]["unused"] and not rows["acquiring_rate"]["unused"]
    # 011: «Предположение» — допущение; короткие темы не обрезаются в колонке.
    assert rows["planning_payment_method"]["kind"] == "допущение"
    assert rows["acquiring_rate"]["theme"] == "2. Комиссии" and rows["code_price"]["theme"] == "4. Упаковка"
    assert "Сохранять 30 тыс." in rows["min_cash_reserve"]["comment"]
    assert rows["ip_fixed_contrib_2027"]["label"] == "Фиксированные взносы полного 2027 года"
    assert rows["min_cash_reserve"]["since"] in ("с импорта v1.5",) or rows["min_cash_reserve"]["since"].endswith(".2026")
    # Ставка для карт после пометки «не используется» — та же: acquiring_rate.
    assert await fin.fetchval("select fin.payment_rate_for('bank_card')") == await fin.fetchval("select fin.payment_rate()")


async def test_investments_have_their_own_colors(fin):
    # 018: принтер, весы и ККТ в стопке «Куда ушли деньги» больше не одного синего.
    colors = {r["item"]: r["color"] for r in await fin.fetch(
        "select item, color from fin.budget_articles where cash_class = 'Вложения'")}
    assert colors == {"Термопринтер": "#1A5FB4", "Весы": "#99C1F1", "ККТ, ФН, фискализация": "#613583"}
