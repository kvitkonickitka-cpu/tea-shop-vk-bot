"""Дашборд «Сводка» в Metabase (вкладки: Обзор, Деньги, Воронка, Заказы, Удержание, Ассортимент,
Себестоимость и маржа). Повторный запуск собирает вопросы заново в новой коллекции
«Дашборды», а сам дашборд обновляет на месте — ссылка на него не меняется; прежняя
коллекция с прежними вопросами уходит в архив. Все вопросы — SQL к витринам analytics
и finance (без ПДн)."""
import time
import uuid
from mbapi import call, BASE

DB = 2
COLLECTION = "Дашборды"
SITE = BASE[:-4]

# id полей с датой, к которым привязан фильтр «Даты» (из /api/database/2/metadata)
F = {
    "cash.period_start": 183, "metrics.period_start": 220, "ddc.period_start": 212,
    "unclassified.operation_date": 235,
    "funnel.created_date_msk": 96, "orders.created_date_msk": 135, "orders.paid_date_msk": 137,
    "items.created_date_msk": 127, "touches.sent_date_msk": 175, "clients.first_contact_date_msk": 81,
    "cash.period_type": 182, "metrics.period_type": 219, "daily.day": 246,
}
# Поля для вопросов-конструкторов (mbql): v_cash_summary, v_metrics, v_orders.
CASH_CLOSING, CASH_CFO, CASH_FCF, METRICS_RUNWAY = 186, 190, 192, 225
ORDERS_TOTAL, ORDERS_IS_PAID, ORDERS_IS_REFUNDED = 154, 142, 143



def sync_fields(wanted):
    """id полей новых представлений — по имени, а не вписанные руками.

    Новое представление Metabase видит только после синхронизации схемы, поэтому
    сначала просим её, потом ждём, пока поля появятся. Не появились — стоп до
    того, как прежняя коллекция уйдёт в архив.
    """
    call("POST", f"/database/{DB}/sync_schema", {})
    found = {}
    for _ in range(40):
        meta = call("GET", f"/database/{DB}/metadata")
        found = {key: f["id"] for t in meta["tables"] if t.get("schema") == "analytics"
                 for f in t["fields"] for key, (table, column) in wanted.items()
                 if t["name"] == table and f["name"] == column}
        if len(found) == len(wanted):
            return found
        time.sleep(3)
    raise SystemExit(f"Metabase не видит {sorted(set(wanted) - set(found))}: бот с новыми представлениями "
                     "задеплоен? Админ → Базы данных → «Синхронизировать схему» и запустить снова.")


F.update(sync_fields({
    "margin.paid_date_msk": ("v_order_margin", "paid_date_msk"),
    "items_margin.paid_date_msk": ("v_order_item_margin", "paid_date_msk"),
    "llm.date_msk": ("v_llm_usage", "date_msk"),
    "bill.day": ("v_anthropic_costs", "day"),
    "steps.started_date_msk": ("v_funnel_dialog_steps", "started_date_msk"),
    "inv.first_invoice_date_msk": ("v_invoice_timing", "first_invoice_date_msk"),
    "dlg.started_date_msk": ("v_dialogs", "started_date_msk"),
}))

RUB = {"suffix": " ₽", "decimals": 0}
# Все строки графика, без «Other»: по умолчанию Metabase сворачивает хвост списка в
# одну строку — сорта и статусы за десятым пропадали из виду. Строки, не влезшие по
# высоте карточки, он всё равно сворачивает: карточке со списком нужна высота.
ALL_ROWS = {"graph.max_categories_enabled": False}
# Плавная кривая вместо ломаной у дневных графиков.
SMOOTH = {"line.interpolate": "cardinal"}


def tag_period():
    return {"period_type": {"id": str(uuid.uuid4()), "name": "period_type", "display-name": "Период",
                            "type": "text", "default": "M", "required": True}}


def tag_dates(field):
    return {"dates": {"id": str(uuid.uuid4()), "name": "dates", "display-name": "Даты", "type": "dimension",
                      "dimension": ["field", F[field], None], "widget-type": "date/all-options"}}


def colfmt(**cols):
    return {"column_settings": {f'["name","{name}"]': fmt for name, fmt in cols.items()}}


CARDS = {}  # ключ -> (id, цель фильтра «Даты», цель фильтра «Период»); None — фильтр не привязан


def card(key, coll, name, sql, display, viz=None, date_field=None, period=False, description=None):
    tags = {}
    if period:
        tags.update(tag_period())
    if date_field:
        tags.update(tag_dates(date_field))
    body = {"name": name, "display": display, "visualization_settings": viz or {}, "collection_id": coll,
            "description": description,
            "dataset_query": {"type": "native", "database": DB,
                              "native": {"query": sql.strip(), "template-tags": tags}}}
    cid = call("POST", "/card", body)["id"]
    CARDS[key] = (cid,
                  ["dimension", ["template-tag", "dates"], {"stage-number": 0}] if date_field else None,
                  ["variable", ["template-tag", "period_type"]] if period else None)
    return cid


def mbql(key, coll, name, table, aggregation, date_field, fmt=None, filter_=None, period_field=None, description=None):
    """Число с динамикой к прошлому месяцу — вопросом-конструктором, а не SQL.

    Группировка по месяцу у конструктора даёт подпись «окт. 2026»; у SQL-вопроса
    Metabase пишет «октябрь 1, 2026» и формат даты у smartscalar не применяет.
    Фильтр «Период» здесь — по полю period_type, поэтому сам вопрос вне дашборда
    смешивает месяцы, кварталы и годы: смотреть его на «Сводке», где Период = M.
    """
    query = {"source-table": table, "aggregation": [aggregation],
             "breakout": [["field", F[date_field], {"temporal-unit": "month"}]]}
    if filter_:
        query["filter"] = filter_
    viz = {"column_settings": {f'["name","{aggregation[0]}"]': fmt}} if fmt else {}
    body = {"name": name, "display": "smartscalar", "visualization_settings": viz, "collection_id": coll,
            "description": description, "dataset_query": {"type": "query", "database": DB, "query": query}}
    cid = call("POST", "/card", body)["id"]
    CARDS[key] = (cid,
                  ["dimension", ["field", F[date_field], None], {"stage-number": 0}],
                  ["dimension", ["field", F[period_field], None], {"stage-number": 0}] if period_field else None)
    return cid


# С 21.10.2026 «Сводку» заменили «Пульс» и «Служебное» (dash_build.py) в той же коллекции
# «Дашборды». Пересборка ниже архивирует всю коллекцию — вместе с ними, — поэтому стоп.
if any(d.get("name") in ("Пульс", "Служебное") for d in call("GET", "/dashboard/")):
    raise SystemExit("«Сводка» заменена «Пульсом» и «Служебным» — собирайте их: dash_build.py. "
                     "build.py архивировал бы коллекцию «Дашборды» вместе с ними.")

# --- коллекция -----------------------------------------------------------------------------
# Прежнюю коллекцию — в архив в самом конце, когда дашборд уже переехал в новую: так
# дашборд остаётся тем же (та же ссылка), а архивируются только прежние вопросы.
OLD_COLLECTIONS = [c["id"] for c in call("GET", "/collection")
                   if c.get("name") == COLLECTION and not c.get("archived") and c.get("id") != "root"]
OLD_DASHBOARD = next((item["id"] for cid in OLD_COLLECTIONS
                      for item in call("GET", f"/collection/{cid}/items?models=dashboard").get("data", [])
                      if item.get("name") == "Сводка"), None)
coll = call("POST", "/collection", {"name": COLLECTION, "description": "Собрано Claude через API. Источник — схемы analytics и finance."})["id"]

# --- Деньги --------------------------------------------------------------------------------
CS = "from finance.v_cash_summary where period_type = {{period_type}} [[and {{dates}}]]"
mbql("bal", coll, "Денег на счетах", 16, ["sum", ["field", CASH_CLOSING, None]], "cash.period_start", RUB,
     period_field="cash.period_type")
mbql("fcf", coll, "Свободный денежный поток (FCF)", 16, ["sum", ["field", CASH_FCF, None]], "cash.period_start", RUB,
     period_field="cash.period_type", description="Операционный поток минус инвестиции (CAPEX).")
mbql("cfo", coll, "Операционный поток", 16, ["sum", ["field", CASH_CFO, None]], "cash.period_start", RUB,
     period_field="cash.period_type")
mbql("runway", coll, "На сколько хватит денег, мес.", 19, ["max", ["field", METRICS_RUNWAY, None]],
     "metrics.period_start", {"decimals": 1}, period_field="metrics.period_type",
     description="Остаток денег, делённый на средний операционный отток за последние месяцы. Пусто — оттока нет.")
card("daily_bal", coll, "Деньги на счетах по дням",
     # До первого дня с деньгами витрина тянет нули с начала года — ровная линия на полграфика.
     'select day as "День", balance as "Остаток" from finance.v_daily_balance_total\n'
     "where day >= (select min(day) from finance.v_daily_balance_total where balance <> 0)\n"
     "[[and {{dates}}]] order by 1",
     "line", {"graph.dimensions": ["День"], "graph.metrics": ["Остаток"], **SMOOTH, **colfmt(**{"Остаток": RUB}),
             # Без точек на каждый день и ось не от нуля: иначе спуски остатка теряются.
             "line.marker_enabled": False, "graph.y_axis.unpin_from_zero": True},
     "daily.day", description="Остаток на всех счетах на конец каждого дня (выписка Т-Банка).")
card("fcf_chart", coll, "FCF по периодам",
     f"select period_start as \"Период\", fcf as \"FCF\", fcf_rolling_3m as \"FCF, среднее за 3 мес.\" {CS} order by 1",
     "combo", {"graph.dimensions": ["Период"], "graph.metrics": ["FCF", "FCF, среднее за 3 мес."],
               "series_settings": {"FCF": {"display": "bar"}, "FCF, среднее за 3 мес.": {"display": "line"}}},
     "cash.period_start", True)
card("waterfall", coll, "Из чего сложился поток",
     "with s as (select * from finance.v_cash_summary where period_type = {{period_type}} [[and {{dates}}]])\n"
     "select \"Статья\", \"Сумма\" from (\n"
     "  select 1 as o, 'Операционный поток' as \"Статья\", sum(cfo) as \"Сумма\" from s\n"
     "  union all select 2, 'Инвестиции (CAPEX)', sum(capex) from s\n"
     "  union all select 3, 'Кредит получен', sum(loan_in) from s\n"
     "  union all select 4, 'Погашение тела кредита', sum(loan_principal_out) from s\n"
     ") x order by o",
     "waterfall", {"graph.dimensions": ["Статья"], "graph.metrics": ["Сумма"], "waterfall.show_total": True},
     "cash.period_start", True, "Итог — поток, который остаётся собственникам (FCFE).")
card("costs", coll, "Структура расходов",
     "select period_start as \"Месяц\", grp as \"Группа\", -sum(amount) as \"Расход\" from finance.v_ddc "
     "where section = 'CFO' and amount < 0 [[and {{dates}}]] group by 1, 2 order by 1",
     "bar", {"graph.dimensions": ["Месяц", "Группа"], "graph.metrics": ["Расход"], "stackable.stack_type": "stacked"},
     "ddc.period_start")
card("income", coll, "Каналы поступлений",
     "select period_start as \"Месяц\", article as \"Статья\", sum(amount) as \"Поступления\" from finance.v_ddc "
     "where section = 'CFO' and amount > 0 [[and {{dates}}]] group by 1, 2 order by 1",
     "bar", {"graph.dimensions": ["Месяц", "Статья"], "graph.metrics": ["Поступления"], "stackable.stack_type": "stacked"},
     "ddc.period_start")
card("ddc", coll, "ДДС: статья × месяц",
     "select section || ' · ' || article as \"Статья\", to_char(period_start, 'YYYY-MM') as \"Месяц\", "
     "sum(amount) as \"Сумма\" from finance.v_ddc where true [[and {{dates}}]] group by 1, 2 order by 1, 2",
     "table", {"table.pivot": True, "table.pivot_column": "Месяц", "table.cell_column": "Сумма"}, "ddc.period_start")
card("uncl_share", coll, "Не разобрано, % суммы",
     f"select round(unclassified_share_amount * 100, 1) as \"Не разобрано, %\" {CS} order by period_start desc limit 1",
     "scalar", colfmt(**{"Не разобрано, %": {"suffix": " %"}}), "cash.period_start", True,
     "Доля оборота, не попавшая ни под одно правило. Норма — меньше 5 %.")
card("uncl", coll, "Не разобрано: операции",
     "select operation_date as \"Дата\", amount as \"Сумма\", direction as \"Направление\", "
     "counterparty_name as \"Контрагент\", purpose as \"Назначение\", issue as \"Что не так\" "
     "from finance.v_unclassified where true [[and {{dates}}]] order by operation_date desc",
     "table", {}, "unclassified.operation_date")
card("hold_sum", coll, "В холде, ₽",
     "select case when bool_or(check_failed) or bool_or(checked_at is null) then null "
     "else sum(holds_signed) end as \"В холде, ₽\" from finance.v_pending_summary",
     "scalar", colfmt(**{"В холде, ₽": {"suffix": " ₽"}}), description=(
         "Операции в холде: банк их ещё не провёл, в ДДС и остатки они не входят. Списания — с минусом. "
         "Пусто — холды проверить не удалось или ещё не проверяли. Обновляется в 07:00 и 21:00 МСК."))
card("hold_tbl", coll, "В холде: операции",
     "select operation_date as \"Дата\", amount_signed as \"Сумма\", counterparty_name as \"Контрагент\", "
     "purpose as \"Назначение\", days_in_hold as \"Дней в холде\" from finance.v_pending "
     "order by operation_date desc",
     "table", {}, description="Ещё не проведённые банком операции. Проведённые уходят отсюда сами.")
card("check", coll, "Сходимость с банком",
     f"select to_char(period_start, 'YYYY-MM') as \"Период\", check_diff as \"Расхождение, ₽\" {CS} "
     "order by period_start desc",
     "table", {}, "cash.period_start", True, "Во всех строках должен быть 0.")

# --- Продажи: Воронка ------------------------------------------------------------------------
INVOICE = "event like 'invoice\\_%' and event not in ('invoice_expired', 'invoice_canceled')"
card("funnel", coll, "Воронка: от диалога до вручения",
     "with e as (\n"
     "  select client_key, case when event = 'dialog_start' then 1 when event = 'consult_started' then 2\n"
     "    when event = 'consult_qualified' then 3 when event = 'draft_created' then 4\n"
     "    when event = 'delivery_quoted' then 5 when event = 'recipient_set' then 6\n"
     f"    when {INVOICE} then 7 when event = 'payment_succeeded' then 8 when event = 'delivered' then 9 end as step\n"
     "  from analytics.v_funnel_events where client_key is not null [[and {{dates}}]])\n"
     "select case step when 1 then '1. Начали диалог' when 2 then '2. Консультация' when 3 then '3. Назван товар'\n"
     "  when 4 then '4. Черновик заказа' when 5 then '5. Посчитана доставка'\n"
     "  when 6 then '6. Указан получатель' when 7 then '7. Выставлен счёт' when 8 then '8. Оплатили'\n"
     "  else '9. Вручено' end as \"Шаг\", count(distinct client_key) as \"Клиентов\"\n"
     "from e where step is not null group by step order by step",
     "funnel", {"funnel.dimension": "Шаг", "funnel.metric": "Клиентов"}, "funnel.created_date_msk",
     description="Сколько разных клиентов дошли до каждого шага за выбранные даты.")
card("conv", coll, "Конверсия диалог → оплата, %",
     "select round(100.0 * count(distinct client_key) filter (where event = 'payment_succeeded')\n"
     "  / nullif(count(distinct client_key) filter (where event = 'dialog_start'), 0), 1) as \"Конверсия, %\"\n"
     "from analytics.v_funnel_events where true [[and {{dates}}]]\n"
     "having count(distinct client_key) filter (where event = 'dialog_start') > 0",
     "scalar", colfmt(**{"Конверсия, %": {"suffix": " %"}}), "funnel.created_date_msk")
card("daily", coll, "Диалоги, счета и оплаты по дням",
     "select created_date_msk as \"День\",\n"
     "  count(distinct client_key) filter (where event = 'dialog_start') as \"Диалогов\",\n"
     f"  count(*) filter (where {INVOICE}) as \"Счетов\",\n"
     "  count(*) filter (where event = 'payment_succeeded') as \"Оплат\"\n"
     "from analytics.v_funnel_events where true [[and {{dates}}]] group by 1 order by 1",
     "line", {"graph.dimensions": ["День"], "graph.metrics": ["Диалогов", "Счетов", "Оплат"], **SMOOTH}, "funnel.created_date_msk")
card("losses", coll, "Где теряем",
     "select event_label as \"Что случилось\", count(*) as \"Раз\" from analytics.v_funnel_events\n"
     "where event in ('invoice_expired', 'payment_declined', 'invoice_canceled', 'canceled_by_client', 'button_stale')\n"
     "[[and {{dates}}]] group by 1 order by 2 desc",
     "row", {"graph.dimensions": ["Что случилось"], "graph.metrics": ["Раз"]}, "funnel.created_date_msk")
card("escal", coll, "Вопросы менеджеру по дням",
     "select created_date_msk as \"День\", count(*) filter (where event = 'escalation_opened') as \"Открыто\",\n"
     "  count(*) filter (where event = 'escalation_closed') as \"Менеджер ответил\"\n"
     "from analytics.v_funnel_events where event like 'escalation\\_%' [[and {{dates}}]] group by 1 order by 1",
     "bar", {"graph.dimensions": ["День"], "graph.metrics": ["Открыто", "Менеджер ответил"]}, "funnel.created_date_msk")

card("steps", coll, "Шаги воронки: дошли, отвалились, время до следующего",
     "select step_no || '. ' || step_label as \"Шаг\", count(*) filter (where reached) as \"Дошли\",\n"
     "  count(*) filter (where dropped) as \"Отвалились\",\n"
     "  round(100.0 * count(*) filter (where dropped) / nullif(count(*) filter (where reached), 0), 1) as \"Отвал, %\",\n"
     "  round((percentile_cont(0.5) within group (order by minutes_to_next))::numeric, 0) as \"Медиана, мин\",\n"
     "  round((percentile_cont(0.9) within group (order by minutes_to_next))::numeric, 0) as \"90 %, мин\"\n"
     "from analytics.v_funnel_dialog_steps where true [[and {{dates}}]] group by step_no, step_label order by step_no",
     "table", colfmt(**{"Отвал, %": {"suffix": " %"}}), "steps.started_date_msk",
     description="По сессиям диалога: сколько дошли до шага, сколько на нём остановились и сколько минут "
                 "до следующего шага. Даты — начало сессии.")
card("by_source", coll, "Диалог → оплата по источнику",
     "select case session_source_kind when 'ad' then 'Реклама' when 'referral' then 'Друг' when 'own' then 'Свои'\n"
     "  else 'Органика' end as \"Источник\", count(*) as \"Диалогов\",\n"
     "  count(*) filter (where consult_started) as \"Консультаций\", count(*) filter (where is_paid) as \"Оплат\",\n"
     "  round(100.0 * count(*) filter (where is_paid) / nullif(count(*), 0), 1) as \"Конверсия, %\"\n"
     "from analytics.v_dialogs where true [[and {{dates}}]] group by 1 order by 2 desc",
     "table", colfmt(**{"Конверсия, %": {"suffix": " %"}}), "dlg.started_date_msk")
card("draft_inv", coll, "Черновик → счёт, медиана минут",
     "select round((percentile_cont(0.5) within group (order by minutes_draft_to_invoice))::numeric, 0) as \"Минут\"\n"
     "from analytics.v_invoice_timing where minutes_draft_to_invoice is not null [[and {{dates}}]]",
     "scalar", colfmt(**{"Минут": {"suffix": " мин"}}), "inv.first_invoice_date_msk")
card("expired_share", coll, "Доля заказов с истёкшим счётом",
     "select round(100.0 * avg(case when had_expired then 1 else 0 end), 1) as \"Доля, %\"\n"
     "from analytics.v_invoice_timing where true [[and {{dates}}]] having count(*) > 0",
     "scalar", colfmt(**{"Доля, %": {"suffix": " %"}}), "inv.first_invoice_date_msk")
card("llm_dialog", coll, "Claude на диалог, оплату и нового покупателя, ₽",
     "select to_char(started_date_msk, 'YYYY-MM') as \"Месяц\", count(*) as \"Диалогов\",\n"
     "  round(sum(llm_cost_rub) / nullif(count(*), 0), 1) as \"₽ на диалог\",\n"
     "  round(sum(llm_cost_rub) / nullif(count(*) filter (where is_paid), 0), 0) as \"₽ на оплату\",\n"
     "  round(sum(llm_cost_rub) / nullif(count(*) filter (where is_first_purchase), 0), 0) as \"₽ на нового покупателя\"\n"
     "from analytics.v_dialogs where true [[and {{dates}}]] group by 1 order by 1 desc",
     "table", {}, "dlg.started_date_msk",
     description="Весь расход Claude за месяц делится на оплаты: диалоги без покупки входят в цену покупки. "
                 "Курс — вводная usd_rub; пусто — курса нет.")

# Ворота пилота — из финмодели (fin.v_pilot_gates), та же карточка, что на «Финансах».
card("gates", coll, "Ворота пилота",
     "select gate as \"Ворота\", check_name as \"Проверка\",\n"
     "  case when unit = '%' then fin.fmt_pct(value) else fin.fmt_rub(value) end as \"Сейчас\",\n"
     "  case when unit = '%' then fin.fmt_pct(threshold) else fin.fmt_rub(threshold) end as \"Порог\",\n"
     "  case status when 'зелёный' then '🟢 зелёный' when 'красный' then '🔴 красный' else '⚪ нет данных' end as \"Статус\",\n"
     "  period_end as \"Срок\" from fin.v_pilot_gates order by sort",
     "table", {}, description="Неделя 2, неделя 6 и М3 пилота. Пороги — вводные gate_* на «Финансах».")

# --- Продажи: Заказы ---------------------------------------------------------------------------
PAID = "from analytics.v_orders where is_paid and not is_refunded [[and {{dates}}]]"
PAID_MBQL = ["and", ["=", ["field", ORDERS_IS_PAID, None], True], ["=", ["field", ORDERS_IS_REFUNDED, None], False]]
mbql("rev", coll, "Выручка", 14, ["sum", ["field", ORDERS_TOTAL, None]], "orders.paid_date_msk", RUB, PAID_MBQL,
     description="Оплаченные заказы без возвращённых, по дате оплаты.")
mbql("ord_cnt", coll, "Оплаченных заказов", 14, ["count"], "orders.paid_date_msk", None, PAID_MBQL)
mbql("avg", coll, "Средний чек", 14, ["avg", ["field", ORDERS_TOTAL, None]], "orders.paid_date_msk", RUB, PAID_MBQL)
card("subsidy", coll, "Доставка за наш счёт",
     f"select coalesce(sum(carrier_delivery_cost) filter (where free_delivery), 0) as \"Доставка за наш счёт\" {PAID}",
     "scalar", colfmt(**{"Доставка за наш счёт": RUB}), "orders.paid_date_msk",
     description="Цена перевозчика по заказам с бесплатной для клиента доставкой.")
card("rev_daily", coll, "Выручка по дням",
     f"select paid_date_msk as \"День\", sum(total) as \"Выручка\", count(*) as \"Заказов\" {PAID} group by 1 order by 1",
     "line", {"graph.dimensions": ["День"], "graph.metrics": ["Выручка"], **SMOOTH}, "orders.paid_date_msk")
card("channels", coll, "Заказы по каналам",
     f"select channel_label as \"Канал\", count(*) as \"Заказов\", sum(total) as \"Выручка\" {PAID} group by 1 order by 3 desc",
     "row", {"graph.dimensions": ["Канал"], "graph.metrics": ["Выручка"]}, "orders.paid_date_msk")
card("carriers", coll, "Перевозчики",
     f"select coalesce(carrier, 'не указан') as \"Перевозчик\", count(*) as \"Заказов\" {PAID} group by 1 order by 2 desc",
     "pie", {"pie.dimension": "Перевозчик", "pie.metric": "Заказов"}, "orders.paid_date_msk")
card("statuses", coll, "Статусы заказов",
     "select status_label as \"Статус\", count(*) as \"Заказов\" from analytics.v_orders where true [[and {{dates}}]] "
     "group by 1 order by 2 desc",
     "row", {"graph.dimensions": ["Статус"], "graph.metrics": ["Заказов"], **ALL_ROWS}, "orders.created_date_msk",
     description="Все заказы с выставленным счётом, по дате создания.")
card("orders_tbl", coll, "Заказы",
     "select order_id as \"№\", created_date_msk as \"Создан\", paid_date_msk as \"Оплачен\", status_label as \"Статус\",\n"
     "  channel_label as \"Канал\", carrier as \"Перевозчик\", total as \"Сумма\", payment_method as \"Способ оплаты\",\n"
     "  expected_delivery_date as \"Обещали к\", delivered_date_msk as \"Вручён\"\n"
     "from analytics.v_orders where true [[and {{dates}}]] order by order_id desc",
     "table", {}, "orders.created_date_msk")

# --- Продажи: Удержание ------------------------------------------------------------------------
card("new_rep", coll, "Первые и повторные покупки",
     "select date_trunc('month', paid_date_msk)::date as \"Месяц\",\n"
     "  case when client_paid_order_number = 1 then 'Первая покупка' else 'Повторная' end as \"Покупка\", count(*) as \"Заказов\"\n"
     f"{PAID} group by 1, 2 order by 1",
     "bar", {"graph.dimensions": ["Месяц", "Покупка"], "graph.metrics": ["Заказов"], "stackable.stack_type": "stacked"},
     "orders.paid_date_msk")
card("rep_share", coll, "Доля повторных покупок, %",
     f"select round(100.0 * count(*) filter (where client_paid_order_number > 1) / nullif(count(*), 0), 1) as \"Повторные, %\" {PAID} having count(*) > 0",
     "scalar", colfmt(**{"Повторные, %": {"suffix": " %"}}), "orders.paid_date_msk")
card("touches", coll, "Касания после покупки",
     "select touch_label as \"Касание\", count(*) as \"Отправлено\",\n"
     "  round(100.0 * avg(pressed::int), 1) as \"Нажали, %\", round(100.0 * avg(ordered_7d::int), 1) as \"Заказали за 7 дней, %\",\n"
     "  round(100.0 * avg(opted_out_2d::int), 1) as \"Отписались, %\"\n"
     "from analytics.v_touches where true [[and {{dates}}]] group by 1 order by 2 desc",
     "table", {}, "touches.sent_date_msk")
card("buyers", coll, "Клиенты по числу покупок",
     "select case when paid_orders_count = 0 then '0 — не купили' when paid_orders_count = 1 then '1'\n"
     "  when paid_orders_count = 2 then '2' else '3 и больше' end as \"Покупок\", count(*) as \"Клиентов\"\n"
     "from analytics.v_clients where true [[and {{dates}}]] group by 1 order by 1",
     "bar", {"graph.dimensions": ["Покупок"], "graph.metrics": ["Клиентов"]}, "clients.first_contact_date_msk",
     description="Клиенты по дате первого контакта.")
card("ratings", coll, "Оценки заказов",
     "select rating_label as \"Оценка\", count(*) as \"Заказов\" from analytics.v_orders where rating is not null "
     "[[and {{dates}}]] group by 1 order by 2 desc",
     "pie", {"pie.dimension": "Оценка", "pie.metric": "Заказов"}, "orders.paid_date_msk")

# --- Продажи: Ассортимент ----------------------------------------------------------------------
ITEMS = "from analytics.v_order_items join analytics.v_orders o using (order_id) where o.is_paid and not o.is_refunded [[and {{dates}}]]"
card("by_product", coll, "Выручка по сортам",
     f"select v_order_items.product as \"Сорт\", sum(v_order_items.amount) as \"Выручка\" {ITEMS} group by 1 order by 2 desc limit 20",
     "row", {"graph.dimensions": ["Сорт"], "graph.metrics": ["Выручка"], **ALL_ROWS}, "items.created_date_msk")
card("by_pack", coll, "Пачки по фасовкам",
     f"select coalesce(v_order_items.pack, 'без фасовки') as \"Фасовка\", sum(v_order_items.quantity) as \"Пачек\" {ITEMS} group by 1 order by 2 desc",
     "pie", {"pie.dimension": "Фасовка", "pie.metric": "Пачек"}, "items.created_date_msk")
card("assort_tbl", coll, "Сорта и фасовки",
     f"select v_order_items.product as \"Сорт\", v_order_items.pack as \"Фасовка\", sum(v_order_items.quantity) as \"Пачек\", sum(v_order_items.amount) as \"Выручка\",\n"
     f"  count(distinct v_order_items.order_id) as \"Заказов\" {ITEMS} group by 1, 2 order by 4 desc",
     "table", {}, "items.created_date_msk")
card("upsell", coll, "Допродажа принята, %",
     "select round(100.0 * count(*) filter (where upsell_accepted) / nullif(count(*) filter (where upsell_offered), 0), 1)\n"
     "  as \"Принята, %\" from analytics.v_orders where true [[and {{dates}}]]\n"
     "having count(*) filter (where upsell_offered) > 0",
     "scalar", colfmt(**{"Принята, %": {"suffix": " %"}}), "orders.created_date_msk",
     description="Из заказов, где предлагали товар из «С чем советуем».")

# --- Себестоимость и маржа: справочники из таблицы (Товары, Себестоимость, Настройки, Приходы) ---
RUB2 = {"suffix": " ₽", "decimals": 2}
PCT = {"suffix": " %", "decimals": 1}
MARGIN = "from analytics.v_order_margin where is_paid and not is_refunded [[and {{dates}}]]"
LINES = ("from analytics.v_order_item_margin where is_paid and not is_refunded [[and {{dates}}]]")
card("m_margin", coll, "Маржинальная прибыль",
     f'select coalesce(sum(contribution_margin), 0) as "Маржинальная прибыль" {MARGIN}',
     "scalar", colfmt(**{"Маржинальная прибыль": RUB}), "margin.paid_date_msk",
     description="Выручка оплаченных заказов без возвратов минус себестоимость, эквайринг, чеки, УСН, "
                 "доставка, прочие расходы и Claude API — по ставкам «Настроек» на дату оплаты.")
card("m_pct", coll, "Маржа, % выручки",
     f'select round(100 * sum(contribution_margin) / nullif(sum(total), 0), 1) as "Маржа, %" {MARGIN} having count(*) > 0',
     "scalar", colfmt(**{"Маржа, %": PCT}), "margin.paid_date_msk")
card("m_cogs", coll, "Себестоимость проданного",
     f'select coalesce(sum(cogs), 0) as "Себестоимость" {MARGIN}',
     "scalar", colfmt(**{"Себестоимость": RUB}), "margin.paid_date_msk",
     description="Себестоимость товаров в оплаченных заказах — по вкладке «Себестоимость» на дату оплаты.")
card("m_avg", coll, "Маржа на заказ",
     f'select round(avg(contribution_margin), 0) as "Маржа на заказ" {MARGIN} having count(*) > 0',
     "scalar", colfmt(**{"Маржа на заказ": RUB}), "margin.paid_date_msk")
card("m_waterfall", coll, "Из чего сложилась маржа",
     f"with m as (select * {MARGIN})\n"
     'select "Статья", "Сумма" from (\n'
     "  select 1 as o, 'Выручка' as \"Статья\", sum(total) as \"Сумма\" from m\n"
     "  union all select 2, 'Себестоимость товаров', -sum(cogs) from m\n"
     "  union all select 3, 'Эквайринг', -sum(acquiring) from m\n"
     "  union all select 4, 'Чеки', -sum(fiscalization) from m\n"
     "  union all select 5, 'Резерв УСН', -sum(usn_reserve) from m\n"
     "  union all select 6, 'Доставка', -sum(delivery_expense) from m\n"
     "  union all select 7, 'Прочие на заказ', -sum(other_variable) from m\n"
     "  union all select 8, 'Claude API', -sum(claude_api) from m\n"
     ") x where \"Сумма\" is not null order by o",
     "waterfall", {"graph.dimensions": ["Статья"], "graph.metrics": ["Сумма"], "waterfall.show_total": True},
     "margin.paid_date_msk", description="Итог — маржинальная прибыль за выбранные даты.")
card("m_orders", coll, "Маржа по заказам",
     'select order_id as "№", paid_date_msk as "Оплачен", total as "Выручка", cogs as "Себестоимость",\n'
     '  acquiring + fiscalization as "Эквайринг и чеки", usn_reserve as "УСН", delivery_expense as "Доставка",\n'
     '  other_variable + claude_api as "Прочее", contribution_margin as "Маржа", margin_pct as "Маржа, %",\n'
     '  lines_without_cost as "Строк без себестоимости"\n'
     f"{MARGIN} order by order_id desc",
     "table", colfmt(**{"Маржа, %": PCT}), "margin.paid_date_msk",
     description="«Строк без себестоимости» больше нуля — товар не связан со справочником или себестоимость "
                 "не задана, и маржа заказа завышена.")
card("m_by_product", coll, "Маржа по товарам: продажи",
     'select coalesce(product_name, item_name) as "Товар", sum(quantity) as "Продано, шт", sum(amount) as "Выручка",\n'
     '  sum(cogs) as "Себестоимость", sum(gross_margin) as "Валовая маржа",\n'
     '  round(100 * sum(gross_margin) / nullif(sum(amount), 0), 1) as "Валовая маржа, %"\n'
     f"{LINES} and has_cost group by 1 order by 5 desc",
     "table", colfmt(**{"Валовая маржа, %": PCT}), "items_margin.paid_date_msk",
     description="Валовая — выручка товара минус его себестоимость, без эквайринга, налога и доставки.")
card("m_missing", coll, "Продано без себестоимости",
     'select item_name as "Товар в заказе", sum(quantity) as "Продано, шт", sum(amount) as "Выручка",\n'
     "  case when product_code is null then 'не связан со справочником' else 'нет себестоимости на дату' end as \"Почему\"\n"
     f"{LINES} and not has_cost group by 1, 4 order by 3 desc",
     "table", {}, "items_margin.paid_date_msk",
     description="Не связан — «Название в каталоге бота» на вкладке «Товары» не совпадает с названием в "
                 "«Каталоге SKU».")
card("ref_products", coll, "Справочник: себестоимость и маржа на единицу",
     'select code as "Код", name as "Товар", kind as "Тип", retail_price as "Розница", unit_cost as "Себестоимость",\n'
     '  unit_margin as "Маржа, ₽", margin_pct as "Маржа, %",\n'
     "  case when in_bot_catalog then 'да' else '' end as \"Продаёт бот\", bot_price as \"Цена бота\",\n"
     '  bot_unit_margin as "Маржа по цене бота, ₽", bot_margin_pct as "Маржа по цене бота, %", components as "Состав"\n'
     "from analytics.v_ref_products where active order by kind, unit_margin desc nulls last",
     "table", colfmt(**{"Маржа, %": PCT, "Маржа по цене бота, %": PCT}),
     description="Вкладки «Товары», «Состав» и «Себестоимость» таблицы каталога, себестоимость — действующая "
                 "сегодня. Маржа здесь — только цена минус себестоимость, без эквайринга, налога и доставки.")
card("ref_margin_chart", coll, "Маржа на единицу по рознице",
     'select name as "Товар", unit_margin as "Маржа, ₽" from analytics.v_ref_products\n'
     "where active and unit_margin is not null order by 2 desc",
     "row", {"graph.dimensions": ["Товар"], "graph.metrics": ["Маржа, ₽"], **ALL_ROWS, **colfmt(**{"Маржа, ₽": RUB})},
     description="Розничная цена справочника минус себестоимость единицы.")
card("ref_settings", coll, "Настройки, действующие сегодня",
     'select name as "Параметр", case when unit = \'доля\' then round(value * 100, 2)::text || \' %\'\n'
     "  else trim(trailing '.' from trim(trailing '0' from value::text)) || coalesce(' ' || unit, '') end as \"Значение\",\n"
     '  valid_from as "Действует с", source as "Источник", comment as "Комментарий", key as "Ключ"\n'
     "from analytics.v_ref_settings order by key",
     "table", {}, description="Вкладка «Настройки»: по каждому ключу — строка с самой поздней наступившей датой.")
card("ref_costs", coll, "История себестоимости",
     'select name as "Товар", valid_from as "С", valid_to as "По", unit_cost as "Себестоимость",\n'
     "  case when is_current then 'действует' else '' end as \"Сейчас\", source as \"Источник\"\n"
     "from analytics.v_ref_costs order by name, valid_from desc",
     "table", colfmt(**{"Себестоимость": RUB2}))
card("ref_stock", coll, "Приходы и инвентаризация",
     'select move_date as "Дата", operation as "Операция", name as "Сорт", quantity as "Количество", unit as "Ед.",\n'
     '  amount as "Сумма", document as "Документ", comment as "Комментарий"\n'
     "from analytics.v_ref_stock_moves order by move_date desc, name",
     "table", colfmt(**{"Сумма": RUB2}))

# --- Claude API: расход в долларах (llm_usage) ---------------------------------------------
USD = {"prefix": "$", "decimals": 2}
for days in (7, 14, 30):
    card(f"api_{days}d", coll, f"Claude API за {days} дней, $",
         f'select coalesce(round(sum(cost_usd), 2), 0) as "Расход, $" from analytics.v_llm_usage '
         f"where at >= now() - interval '{days} days'",
         "scalar", colfmt(**{"Расход, $": USD}),
         description=f"Расход на Claude API за последние {days} дней, по ценам Anthropic: все запросы бота, "
                     "ассистента Ops и пересказов, включая тестовые диалоги. Фильтр «Даты» здесь не действует.")
card("api_daily", coll, "Claude API по дням, $",
     'select date_msk as "День", operation as "На что", round(sum(cost_usd), 4) as "Расход, $"\n'
     "from analytics.v_llm_usage where true [[and {{dates}}]] group by 1, 2 order by 1",
     "bar", {"graph.dimensions": ["День", "На что"], "graph.metrics": ["Расход, $"],
             "stackable.stack_type": "stacked", **colfmt(**{"Расход, $": USD})}, "llm.date_msk")
card("api_tbl", coll, "Claude API: на что ушло за 30 дней",
     'select operation as "На что", model as "Модель", count(*) as "Запросов",\n'
     '  round(sum(cost_usd), 2) as "Расход, $", round(avg(cost_usd), 4) as "За запрос, $",\n'
     '  sum(input_tokens) as "Вход", sum(output_tokens) as "Выход", sum(cache_read_tokens) as "Из кэша",\n'
     '  sum(cache_write_tokens) as "В кэш",\n'
     '  round(100.0 * sum(cache_read_tokens) / nullif(sum(input_tokens + cache_read_tokens + cache_write_tokens), 0), 1)\n'
     '    as "Вход из кэша, %"\n'
     "from analytics.v_llm_usage where at >= now() - interval '30 days' group by 1, 2 order by 4 desc nulls last",
     "table", colfmt(**{"Расход, $": USD, "За запрос, $": {"prefix": "$", "decimals": 4}}),
     description="Вход из кэша — доля входных токенов, прочитанных из кэша промпта: он в 10 раз дешевле.")

# --- Счёт Anthropic (Admin API cost_report) — то, что выставят к оплате ------------------------
for days in (7, 14, 30):
    card(f"bill_{days}d", coll, f"Счёт Anthropic за {days} дней, $",
         f'select coalesce(round(sum(amount_usd), 2), 0) as "Счёт, $" from analytics.v_anthropic_costs '
         f"where day >= current_date - {days - 1}",
         "scalar", colfmt(**{"Счёт, $": USD}),
         description=f"Сколько Anthropic выставит к оплате за последние {days} дней — по всей организации "
                     "(бот, ассистент Ops, консоль). Обновляется раз в час, сегодняшний день — с задержкой.")
card("bill_daily", coll, "Счёт Anthropic по дням, $",
     'select day as "День", case token_type when \'output_tokens\' then \'выход\'\n'
     "  when 'uncached_input_tokens' then 'вход' when 'cache_read_input_tokens' then 'чтение из кэша'\n"
     "  when 'cache_creation.ephemeral_5m_input_tokens' then 'запись в кэш'\n"
     "  when 'cache_creation.ephemeral_1h_input_tokens' then 'запись в кэш (1 ч)'\n"
     "  else coalesce(cost_type, 'прочее') end as \"Статья\", round(sum(amount_usd), 4) as \"Счёт, $\"\n"
     "from analytics.v_anthropic_costs where true [[and {{dates}}]] group by 1, 2 order by 1",
     "bar", {"graph.dimensions": ["День", "Статья"], "graph.metrics": ["Счёт, $"],
             "stackable.stack_type": "stacked", **colfmt(**{"Счёт, $": USD})}, "bill.day")
card("bill_vs", coll, "Счёт Anthropic и учёт бота по дням",
     "with bill as (select day, sum(amount_usd) as usd from analytics.v_anthropic_costs group by 1),\n"
     "bot as (select (at at time zone 'UTC')::date as day, sum(cost_usd) as usd from analytics.v_llm_usage group by 1)\n"
     'select coalesce(bill.day, bot.day) as "День (UTC)", round(bill.usd, 2) as "Счёт Anthropic, $",\n'
     '  round(bot.usd, 2) as "Учёт бота, $", round(coalesce(bill.usd, 0) - coalesce(bot.usd, 0), 2) as "Разница, $"\n'
     "from bill full join bot on bot.day = bill.day\n"
     "where coalesce(bill.day, bot.day) >= current_date - 29 order by 1 desc",
     "table", colfmt(**{"Счёт Anthropic, $": USD, "Учёт бота, $": USD, "Разница, $": USD}),
     description="Разница — расходы не бота (консоль, другие ключи организации) или запросы, которые бот не "
                 "записал. До 18.10.2026 учёта бота нет — там разница равна счёту.")

# --- проверка: каждый вопрос выполняется без ошибки --------------------------------------------
bad = 0
for key, (cid, _, _) in CARDS.items():
    r = call("POST", f"/card/{cid}/query", {})
    if r.get("status") != "completed":
        bad += 1
        print("ОШИБКА", key, cid, str(r.get("error"))[:300])
print(f"вопросов: {len(CARDS)}, с ошибкой: {bad}")

# --- дашборды ----------------------------------------------------------------------------------
P_DATES = {"id": "dates", "name": "Даты", "slug": "dates", "type": "date/all-options", "sectionId": "date"}
P_PERIOD = {"id": "period", "name": "Период", "slug": "period", "type": "string/=", "sectionId": "string",
            "default": ["M"], "required": True, "values_query_type": "list", "values_source_type": "static-list",
            "values_source_config": {"values": [["M", "Месяц"], ["Q", "Квартал"], ["Y", "Год"]]}}

_seq = [0]


def dc(key, row, col, w, h, tab=None, params=("dates", "period")):
    _seq[0] -= 1
    cid, dates_target, period_target = CARDS[key]
    maps = []
    if dates_target and "dates" in params:
        maps.append({"parameter_id": "dates", "card_id": cid, "target": dates_target})
    if period_target and "period" in params:
        maps.append({"parameter_id": "period", "card_id": cid, "target": period_target})
    d = {"id": _seq[0], "card_id": cid, "row": row, "col": col, "size_x": w, "size_y": h,
         "parameter_mappings": maps, "visualization_settings": {}}
    if tab is not None:
        d["dashboard_tab_id"] = tab
    return d


def make(name, desc, parameters, dashcards, tabs=None, existing=None):
    """Собрать дашборд. `existing` — id прежнего: он переезжает в новую коллекцию и
    получает новые карточки и вкладки, id и ссылка не меняются."""
    if existing:
        did = existing
        call("PUT", f"/dashboard/{did}", {"name": name, "description": desc, "collection_id": coll})
    else:
        did = call("POST", "/dashboard", {"name": name, "description": desc, "collection_id": coll})["id"]
    body = {"parameters": parameters, "dashcards": dashcards}
    if tabs:
        body["tabs"] = tabs
    call("PUT", f"/dashboard/{did}", body)
    print(f"{name}: {SITE}/dashboard/{did}" + (" (тот же дашборд, обновлён)" if existing else " (новый)"))


# Один дашборд «Сводка»: вкладка «Обзор» — главное на одном экране (те же вопросы, что
# на остальных вкладках), дальше вкладки по темам. Фильтр «Период» — только у финансовых.
T = {"Обзор": -1, "Деньги": -2, "Воронка": -3, "Заказы": -4, "Удержание": -5, "Ассортимент": -6,
     "Себестоимость и маржа": -7}
MT = T["Себестоимость и маржа"]
make("Сводка", "Деньги и продажи на одном дашборде. Выписка обновляется в 07:00 и 21:00 МСК, заказы — сразу. "
     "Без тестовых клиентов и заказов.", [P_PERIOD, P_DATES], [
    # Обзор
    dc("bal", 0, 0, 4, 3, T["Обзор"]), dc("runway", 0, 4, 4, 3, T["Обзор"]), dc("rev", 0, 8, 4, 3, T["Обзор"]),
    dc("ord_cnt", 0, 12, 4, 3, T["Обзор"]), dc("avg", 0, 16, 4, 3, T["Обзор"]), dc("conv", 0, 20, 4, 3, T["Обзор"]),
    dc("daily_bal", 3, 0, 24, 6, T["Обзор"]),
    dc("fcf_chart", 9, 0, 12, 6, T["Обзор"]), dc("rev_daily", 9, 12, 12, 6, T["Обзор"]),
    dc("funnel", 15, 0, 12, 7, T["Обзор"]), dc("costs", 15, 12, 12, 7, T["Обзор"]),
    dc("gates", 22, 0, 24, 5, T["Обзор"]),
    # Деньги
    dc("bal", 0, 0, 6, 3, T["Деньги"]), dc("fcf", 0, 6, 6, 3, T["Деньги"]), dc("cfo", 0, 12, 6, 3, T["Деньги"]),
    dc("runway", 0, 18, 6, 3, T["Деньги"]),
    dc("daily_bal", 3, 0, 24, 6, T["Деньги"]),
    dc("fcf_chart", 9, 0, 12, 6, T["Деньги"]), dc("waterfall", 9, 12, 12, 6, T["Деньги"]),
    dc("costs", 15, 0, 12, 6, T["Деньги"]), dc("income", 15, 12, 12, 6, T["Деньги"]),
    dc("ddc", 21, 0, 24, 7, T["Деньги"]),
    dc("uncl_share", 28, 0, 6, 3, T["Деньги"]), dc("check", 31, 0, 6, 4, T["Деньги"]), dc("uncl", 28, 6, 18, 7, T["Деньги"]),
    dc("hold_sum", 35, 0, 6, 3, T["Деньги"]), dc("hold_tbl", 35, 6, 18, 6, T["Деньги"]),
    # Воронка
    dc("funnel", 0, 0, 12, 8, T["Воронка"]), dc("conv", 0, 12, 6, 3, T["Воронка"]),
    dc("losses", 3, 12, 12, 5, T["Воронка"]), dc("daily", 8, 0, 24, 6, T["Воронка"]),
    dc("escal", 14, 0, 24, 5, T["Воронка"]),
    dc("steps", 19, 0, 16, 7, T["Воронка"]), dc("draft_inv", 19, 16, 8, 3, T["Воронка"]),
    dc("expired_share", 22, 16, 8, 4, T["Воронка"]),
    dc("by_source", 26, 0, 12, 5, T["Воронка"]), dc("llm_dialog", 26, 12, 12, 5, T["Воронка"]),
    # Заказы
    dc("rev", 0, 0, 6, 3, T["Заказы"]), dc("ord_cnt", 0, 6, 6, 3, T["Заказы"]), dc("avg", 0, 12, 6, 3, T["Заказы"]),
    dc("subsidy", 0, 18, 6, 3, T["Заказы"]), dc("rev_daily", 3, 0, 24, 6, T["Заказы"]),
    dc("channels", 9, 0, 8, 6, T["Заказы"]), dc("carriers", 9, 8, 8, 6, T["Заказы"]), dc("statuses", 9, 16, 8, 6, T["Заказы"]),
    dc("orders_tbl", 15, 0, 24, 8, T["Заказы"]),
    # Удержание
    dc("new_rep", 0, 0, 16, 6, T["Удержание"]), dc("rep_share", 0, 16, 8, 3, T["Удержание"]),
    dc("ratings", 3, 16, 8, 6, T["Удержание"]), dc("touches", 6, 0, 16, 5, T["Удержание"]),
    dc("buyers", 11, 0, 16, 5, T["Удержание"]),
    # Ассортимент
    dc("by_product", 0, 0, 12, 8, T["Ассортимент"]), dc("by_pack", 0, 12, 6, 6, T["Ассортимент"]),
    dc("upsell", 0, 18, 6, 3, T["Ассортимент"]), dc("assort_tbl", 8, 0, 24, 8, T["Ассортимент"]),
    # Себестоимость и маржа. График маржи на единицу — высокий: строки, не влезшие по
    # высоте, Metabase сворачивает в «Other», а товаров в справочнике 16.
    dc("m_margin", 0, 0, 6, 3, MT), dc("m_pct", 0, 6, 6, 3, MT), dc("m_cogs", 0, 12, 6, 3, MT),
    dc("m_avg", 0, 18, 6, 3, MT),
    dc("api_7d", 3, 0, 8, 3, MT), dc("api_14d", 3, 8, 8, 3, MT), dc("api_30d", 3, 16, 8, 3, MT),
    dc("bill_7d", 6, 0, 8, 3, MT), dc("bill_14d", 6, 8, 8, 3, MT), dc("bill_30d", 6, 16, 8, 3, MT),
    dc("api_daily", 9, 0, 12, 6, MT), dc("bill_daily", 9, 12, 12, 6, MT),
    dc("api_tbl", 15, 0, 12, 6, MT), dc("bill_vs", 15, 12, 12, 6, MT),
    dc("m_waterfall", 21, 0, 12, 12, MT), dc("ref_margin_chart", 21, 12, 12, 12, MT),
    dc("ref_products", 33, 0, 24, 8, MT),
    dc("m_by_product", 41, 0, 14, 6, MT), dc("m_missing", 41, 14, 10, 6, MT),
    dc("m_orders", 47, 0, 24, 8, MT),
    dc("ref_settings", 55, 0, 14, 9, MT), dc("ref_costs", 55, 14, 10, 9, MT),
    dc("ref_stock", 64, 0, 24, 6, MT),
], tabs=[{"id": v, "name": k} for k, v in T.items()], existing=OLD_DASHBOARD)

# Прежние вопросы — в архив. Дашборд уже в новой коллекции, его это не задевает.
for cid in OLD_COLLECTIONS:
    call("PUT", f"/collection/{cid}", {"archived": True})
    print("прежняя коллекция с вопросами в архиве:", cid)
