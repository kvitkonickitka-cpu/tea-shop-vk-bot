"""Финмодель, миграция 013: плановые платежи в прогнозе и бюджете, декларация, FCF.

Выписка — как на бою 24.10.2026: 80 000 ₽ взноса, операционные траты со счёта на
23 485,49 ₽, термопринтер 4 831 ₽, перевод 1 000 ₽ себе (размечен «вывод владельцем»),
два перевода на покупки 3 699 + 14 254 ₽; «мимо счёта» — весы 12 296 ₽ и упаковка
3 699 ₽ (seed fin.expenses). Ручной расчёт владельца на эту дату: операционный поток
−29 142 ₽, вложения −17 127 ₽, FCF −46 269 ₽, финансирование +79 000 ₽.
"""

from __future__ import annotations

import sys
import types
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from tests.test_catalog_forms import render, run_as_form
from tests.test_fin_dashboards import PRINTER, TODAY, _bank
from tests.test_fin_model import fin  # noqa: F401

METABASE = Path(__file__).resolve().parents[1] / "analytics" / "metabase"
OWN_TRANSFER = "own-0921"
BANK_2410 = [
    (date(2026, 9, 10), "in", 80000, "cff_owner_contribution"),
    (date(2026, 9, 14), "out", 5000, "cfo_out_other"),
    (date(2026, 9, 21), "out", 1000, "tech_internal_transfer", OWN_TRANSFER),
    (date(2026, 9, 25), "out", 5053.49, "cfo_out_services"),
    (date(2026, 10, 2), "out", 12959, "cfo_out_services"),
    (date(2026, 10, 6), "out", 4831, "cfo_out_other", PRINTER),
    (date(2026, 10, 16), "out", 473, "cfo_out_packaging"),
    (date(2026, 10, 20), "out", 3699, "tech_internal_transfer"),
    (date(2026, 10, 20), "out", 14254, "tech_internal_transfer"),
]
DECLARATION = "Декларация о соответствии"


@pytest.fixture
async def bank(fin):
    await _bank(fin, BANK_2410, "32730.51")
    await fin.execute("insert into fin.bank_marks (operation_id, transfer_kind, entered_by) "
                      "values ($1, 'вывод владельцем', 'Никита')", OWN_TRANSFER)
    yield fin


async def plan(conn, days_ahead, amount=33000, description="Декларация ТР ТС 021: испытания и пошлина", **extra):
    columns = {"date": TODAY + timedelta(days=days_ahead), "category": "прочее", "amount": Decimal(amount),
               "description": description, "is_plan": True, "paid_from": "счёт ИП", "entered_by": "Никита", **extra}
    names = ", ".join(columns)
    marks = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    return await conn.fetchval(f"insert into fin.expenses ({names}) values ({marks}) returning id", *columns.values())


# --- FCF --------------------------------------------------------------------------------------

@pytest.mark.parametrize("view", ["fin.v_fcf_monthly", "fin.v_fcf_weekly"])
async def test_fcf_matches_the_owners_hand_count(bank, view):
    total = await bank.fetchrow(f"select sum(operating) op, sum(investing) inv, sum(fcf) fcf, sum(financing) fin, "
                                f"sum(unallocated) unalloc from {view}")
    assert total["op"] == Decimal("-29142.49")      # 23 485,49 со счёта + 3 699 упаковки + 1 958 не разнесено
    assert total["inv"] == Decimal("-17127")        # принтер 4 831 + весы 12 296
    assert total["fcf"] == Decimal("-46269.49")
    assert total["fin"] == Decimal("79000")         # вклад 80 000 − перевод себе 1 000
    assert total["unalloc"] == Decimal("1958")
    # Остаток счёта сходится: его изменение = операционный + вложения + финансирование.
    assert total["fcf"] + total["fin"] == Decimal("32730.51")


async def test_fcf_by_month_and_cumulative_lines(bank):
    months = {r["period"]: r for r in await bank.fetch("select * from fin.v_fcf_monthly")}
    sep, oct_ = months[date(2026, 9, 1)], months[date(2026, 10, 1)]
    assert (sep["operating"], sep["investing"], sep["financing"]) == (Decimal("-10053.49"), 0, Decimal("79000"))
    assert (oct_["operating"], oct_["investing"], oct_["unallocated"]) == (Decimal("-19089"), Decimal("-17127"), 1958)
    assert oct_["cum_fcf"] == Decimal("-46269.49") and oct_["cum_contributions"] == 80000
    # До старта заказов нет — и потока на заказ тоже.
    assert oct_["paid_orders"] == 0 and oct_["operating_per_order"] is None


async def test_card_purchases_beyond_transfers_are_financed_by_the_partner(bank):
    await bank.execute("insert into fin.expenses (date, category, amount, description, paid_from, entered_by) "
                       "values ('2026-10-21', 'упаковка', 5000, 'Скотч и наполнитель', 'мимо счёта', 'Илья')")
    oct_ = await bank.fetchrow("select * from fin.v_fcf_monthly where period = '2026-10-01'")
    # Переведено 17 953, куплено 20 995: 3 042 партнёр оплатил своими — финансирование.
    assert oct_["unallocated"] == 0 and oct_["financing"] == Decimal("3042")
    assert oct_["operating"] == Decimal("-13432") - Decimal("3699") - 5000


# --- плановые платежи ----------------------------------------------------------------------------

async def test_declaration_article_and_input(fin):
    assert await fin.fetchval("select fin.inp('declaration_cost')") == 33000
    line = await fin.fetchrow("select * from fin.v_pilot_budget where item = $1", DECLARATION)
    assert line["plan_amount"] == 33000 and line["line_no"] == 16
    assert await fin.fetchval("select cash_class from fin.budget_articles where item = $1", DECLARATION) == "Операционные"


async def test_plan_lowers_the_forecast_on_its_date(bank):
    before = {r["day"]: r["closing"] for r in await bank.fetch("select day, closing from fin.v_runway_daily")}
    plan_id = await plan(bank, 10)
    rows = await bank.fetch("select day, closing, plans_out from fin.v_runway_daily order by day")
    after = {r["day"]: r for r in rows}
    due = TODAY + timedelta(days=10)
    assert after[due]["plans_out"] == 33000
    # Статья бюджета найдена по описанию; её «осталось» уже без плана — дважды не списывается.
    status = await bank.fetchrow("select * from fin.v_pilot_budget_status where item = $1", DECLARATION)
    assert (status["planned"], status["left_amount"]) == (33000, 0)
    # Раньше 33 000 ₽ статьи размазывались по дням до старта, теперь стоят в свою дату:
    # к концу прогноза денег столько же — один и тот же платёж не учтён дважды.
    last = max(before)
    assert round(after[last]["closing"], 2) == round(before[last], 2)
    assert after[due]["closing"] - after[due - timedelta(days=1)]["closing"] <= -33000
    assert sum(r["plans_out"] for r in rows) == 33000
    ahead = await bank.fetch("select * from fin.v_payments_ahead where source = 'план'")
    assert [(r["day"], r["amount"], r["budget_item"]) for r in ahead] == [(due, 33000, DECLARATION)]
    assert plan_id


async def test_plan_in_an_article_without_room_still_counts_once(bank):
    await plan(bank, 5, amount=7000, description="Срочная доработка бота", budget_item="Подрядчик за пилот")
    status = await bank.fetchrow("select * from fin.v_pilot_budget_status where item = 'Подрядчик за пилот'")
    assert status["planned"] == 7000 and status["left_amount"] == status["plan"] - status["spent"] - 7000
    assert await bank.fetchval("select sum(plans_out) from fin.v_runway_daily") == 7000


async def test_fact_closes_the_plan_and_is_not_counted_twice(bank):
    plan_id = await plan(bank, 10)
    await bank.execute("insert into fin.expenses (date, category, amount, description, paid_from, closes_plan_id, "
                       "entered_by) values (current_date, 'прочее', 31500, 'Протоколы испытаний', 'мимо счёта', $1, "
                       "'Никита')", plan_id)
    closed = await bank.fetchrow("select * from fin.v_plans where id = $1", plan_id)
    assert closed["closed"] and closed["closed_by"].startswith("расход №") and closed["due_day"] is None
    assert await bank.fetchval("select sum(plans_out) from fin.v_runway_daily") == 0
    status = await bank.fetchrow("select * from fin.v_pilot_budget_status where item = $1", DECLARATION)
    assert (status["spent"], status["planned"], status["left_amount"]) == (31500, 0, 1500)


async def test_bank_operation_closes_the_plan(bank):
    plan_id = await plan(bank, 3)
    await _bank(bank, BANK_2410 + [(TODAY, "out", 33000, "cfo_out_other", "decl-pay")], "-270.49")
    await bank.execute("insert into fin.bank_marks (operation_id, budget_item, closes_plan_id, entered_by) "
                       "values ('decl-pay', $1, $2, 'Илья')", DECLARATION, plan_id)
    assert (await bank.fetchrow("select * from fin.v_plans where id = $1", plan_id))["closed"]
    assert await bank.fetchval("select sum(plans_out) from fin.v_runway_daily") == 0
    status = await bank.fetchrow("select * from fin.v_pilot_budget_status where item = $1", DECLARATION)
    assert (status["spent"], status["planned"]) == (33000, 0)


async def test_overdue_plan_is_flagged_and_kept_for_tomorrow(bank):
    plan_id = await plan(bank, -3)
    attention = [r["text"] for r in await bank.fetch("select * from fin.v_attention")]
    assert any("не закрыт фактом" in text and "33 000 ₽" in text for text in attention)
    tomorrow = await bank.fetchrow("select * from fin.v_runway_daily order by day limit 1")
    assert tomorrow["plans_out"] == 33000
    await bank.execute("insert into fin.expenses (date, category, amount, paid_from, closes_plan_id, entered_by) "
                       "values (current_date - 2, 'прочее', 33000, 'мимо счёта', $1, 'Никита')", plan_id)
    assert not any("не закрыт фактом" in r["text"] for r in await bank.fetch("select * from fin.v_attention"))
    assert await bank.fetchval("select sum(plans_out) from fin.v_runway_daily") == 0


async def test_only_a_plan_can_be_closed(bank):
    fact = await bank.fetchval("select id from fin.expenses where not is_plan limit 1")
    with pytest.raises(asyncpg.RaiseError, match="не план"):
        await bank.execute("insert into fin.expenses (date, category, amount, paid_from, closes_plan_id, entered_by) "
                           "values (current_date, 'прочее', 1, 'мимо счёта', $1, 'Никита')", fact)
    plan_id = await plan(bank, 3)
    with pytest.raises(asyncpg.RaiseError, match="закрывает факт"):
        await plan(bank, 4, closes_plan_id=plan_id)


async def test_plans_before_start_count_as_money_needed(bank):
    before = await bank.fetchval("select left_before_start from fin.v_money_now")
    await plan(bank, 5)
    # Декларация не была в бюджете до 013 — теперь и статья, и план; дважды не прибавляется.
    assert await bank.fetchval("select left_before_start from fin.v_money_now") == before


async def test_runway_says_whole_forecast_instead_of_a_bare_date(bank):
    await _bank(bank, BANK_2410, "10000000")
    money = await bank.fetchrow("select * from fin.v_money_now")
    assert money["runout_date"] is None
    # 019: коротко — помещается в плитку.
    assert money["runway_text"] == f"позже {money['horizon']:%d.%m.%y}"
    await _bank(bank, BANK_2410, "100")
    assert (await bank.fetchrow("select runway_text from fin.v_money_now"))["runway_text"] == "уже ниже резерва"


# --- формы Metabase -----------------------------------------------------------------------------

@pytest.fixture
def forms(monkeypatch):
    created = []

    def call(method, path, body=None):
        if path == "/dataset":
            return {"data": {"rows": [["1 · 05.11.2026 · 33 000 ₽ · Декларация"]]}}
        if path == "/action":
            created.append(body)
            return {"id": len(created)}
        if path == "/card":
            return {"id": 700}
        raise AssertionError(f"неожиданный вызов {method} {path}")

    fake = types.ModuleType("mbapi")
    fake.BASE, fake.call = "https://stats.example/api", call
    monkeypatch.setitem(sys.modules, "mbapi", fake)
    monkeypatch.syspath_prepend(str(METABASE))
    for name in ("fin_build", "dash_build"):
        sys.modules.pop(name, None)
    import dash_build  # noqa: PLC0415

    dash_build.build_forms(1, 9, defaultdict(int))
    yield {a["name"]: a["dataset_query"]["native"]["query"] for a in created if a.get("type") == "query"}
    for name in ("fin_build", "dash_build"):
        sys.modules.pop(name, None)


async def test_expense_form_sets_article_and_closes_a_plan(bank, forms):
    plan_id = await plan(bank, 10, description="Пошлина")
    sql = forms["Добавить расход"]
    await run_as_form(bank, render(sql, {"date": str(TODAY), "category": "прочее", "amount": 1500,
                                         "description": "Госпошлина", "kind": "факт", "paid_from": "мимо счёта",
                                         "item": DECLARATION, "closes": f"{plan_id} · 03.11.2026 · 1 500 ₽ · Пошлина",
                                         "entered_by": "Никита"}))
    fact = await bank.fetchrow("select * from fin.expenses where description = 'Госпошлина'")
    assert (fact["budget_item"], fact["closes_plan_id"]) == (DECLARATION, plan_id)
    # Без статьи и плана — как раньше.
    await run_as_form(bank, render(sql, {"date": str(TODAY), "category": "упаковка", "amount": 100, "kind": "план",
                                         "paid_from": "мимо счёта", "entered_by": "Илья"}))
    assert await bank.fetchval("select count(*) from fin.v_plans where not closed") == 1


async def test_marking_form_closes_a_plan_with_its_article(bank, forms):
    plan_id = await plan(bank, 3)
    await _bank(bank, BANK_2410 + [(TODAY, "out", 33000, "cfo_out_other", "decl-pay-2")], "-270.49")
    op = await bank.fetchval("select to_char(day, 'DD.MM.YYYY') || ' · x · y · ' || left(operation_id, 8) "
                             "from fin.v_bank_ops where operation_id = 'decl-pay-2'")
    await run_as_form(bank, render(forms["Разметить операцию"], {
        "op": op, "closes": f"{plan_id} · x", "entered_by": "Илья"}))
    mark = await bank.fetchrow("select * from fin.bank_marks where operation_id = 'decl-pay-2'")
    assert (mark["budget_item"], mark["closes_plan_id"]) == (DECLARATION, plan_id)
    assert (await bank.fetchrow("select * from fin.v_plans where id = $1", plan_id))["closed"]
