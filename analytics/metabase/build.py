"""Дашборд «Сводка» в Metabase (вкладки: Обзор, Деньги, Воронка, Заказы, Удержание, Ассортимент). Повторный запуск архивирует прежнюю коллекцию
«Дашборды» и собирает заново. Все вопросы — SQL к витринам analytics и finance (без ПДн)."""
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
}

RUB = {"suffix": " ₽", "decimals": 0}


def tag_period():
    return {"period_type": {"id": str(uuid.uuid4()), "name": "period_type", "display-name": "Период",
                            "type": "text", "default": "M", "required": True}}


def tag_dates(field):
    return {"dates": {"id": str(uuid.uuid4()), "name": "dates", "display-name": "Даты", "type": "dimension",
                      "dimension": ["field", F[field], None], "widget-type": "date/all-options"}}


def colfmt(**cols):
    return {"column_settings": {f'["name","{name}"]': fmt for name, fmt in cols.items()}}


CARDS = {}  # ключ -> (id, есть period_type, есть dates)


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
    CARDS[key] = (cid, period, bool(date_field))
    return cid


# --- коллекция -----------------------------------------------------------------------------
for c in call("GET", "/collection"):
    if c.get("name") == COLLECTION and not c.get("archived") and c.get("id") != "root":
        call("PUT", f"/collection/{c['id']}", {"archived": True})
        print("старая коллекция в архиве:", c["id"])
coll = call("POST", "/collection", {"name": COLLECTION, "description": "Собрано Claude через API. Источник — схемы analytics и finance."})["id"]

# --- Деньги --------------------------------------------------------------------------------
CS = "from finance.v_cash_summary where period_type = {{period_type}} [[and {{dates}}]]"
card("bal", coll, "Денег на счетах", f"select period_start, closing_balance as \"Денег на счетах\" {CS} order by period_start",
     "smartscalar", colfmt(**{"Денег на счетах": RUB}), "cash.period_start", True)
card("fcf", coll, "Свободный денежный поток (FCF)", f"select period_start, fcf as \"FCF\" {CS} order by period_start",
     "smartscalar", colfmt(FCF=RUB), "cash.period_start", True,
     "Операционный поток минус инвестиции (CAPEX).")
card("cfo", coll, "Операционный поток", f"select period_start, cfo as \"Операционный поток\" {CS} order by period_start",
     "smartscalar", colfmt(**{"Операционный поток": RUB}), "cash.period_start", True)
card("runway", coll, "На сколько хватит денег, мес.",
     "select period_start, runway_months as \"Месяцев\" from finance.v_metrics where period_type = {{period_type}} "
     "[[and {{dates}}]] order by period_start",
     "smartscalar", colfmt(**{"Месяцев": {"decimals": 1}}), "metrics.period_start", True,
     "Остаток денег, делённый на средний операционный отток за последние месяцы. Пусто — оттока нет.")
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
card("check", coll, "Сходимость с банком",
     f"select period_start as \"Период\", check_diff as \"Расхождение, ₽\" {CS} order by 1 desc",
     "table", {}, "cash.period_start", True, "Во всех строках должен быть 0.")

# --- Продажи: Воронка ------------------------------------------------------------------------
INVOICE = "event like 'invoice\\_%' and event not in ('invoice_expired', 'invoice_canceled')"
card("funnel", coll, "Воронка: от диалога до вручения",
     "with e as (\n"
     "  select client_key, case when event = 'dialog_start' then 1 when event = 'draft_created' then 2\n"
     "    when event = 'delivery_quoted' then 3 when event = 'recipient_set' then 4\n"
     f"    when {INVOICE} then 5 when event = 'payment_succeeded' then 6 when event = 'delivered' then 7 end as step\n"
     "  from analytics.v_funnel_events where client_key is not null [[and {{dates}}]])\n"
     "select case step when 1 then '1. Начали диалог' when 2 then '2. Черновик заказа' when 3 then '3. Посчитана доставка'\n"
     "  when 4 then '4. Указан получатель' when 5 then '5. Выставлен счёт' when 6 then '6. Оплатили'\n"
     "  else '7. Вручено' end as \"Шаг\", count(distinct client_key) as \"Клиентов\"\n"
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
     "line", {"graph.dimensions": ["День"], "graph.metrics": ["Диалогов", "Счетов", "Оплат"]}, "funnel.created_date_msk")
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

# --- Продажи: Заказы ---------------------------------------------------------------------------
PAID = "from analytics.v_orders where is_paid and not is_refunded [[and {{dates}}]]"
card("rev", coll, "Выручка",
     f"select date_trunc('month', paid_date_msk)::date as \"Месяц\", sum(total) as \"Выручка\" {PAID} group by 1 order by 1",
     "smartscalar", colfmt(**{"Выручка": RUB}), "orders.paid_date_msk",
     description="Оплаченные заказы без возвращённых, по дате оплаты.")
card("ord_cnt", coll, "Оплаченных заказов",
     f"select date_trunc('month', paid_date_msk)::date as \"Месяц\", count(*) as \"Заказов\" {PAID} group by 1 order by 1",
     "smartscalar", {}, "orders.paid_date_msk")
card("avg", coll, "Средний чек",
     f"select date_trunc('month', paid_date_msk)::date as \"Месяц\", round(avg(total)) as \"Средний чек\" {PAID} group by 1 order by 1",
     "smartscalar", colfmt(**{"Средний чек": RUB}), "orders.paid_date_msk")
card("subsidy", coll, "Доставка за наш счёт",
     f"select coalesce(sum(carrier_delivery_cost) filter (where free_delivery), 0) as \"Доставка за наш счёт\" {PAID}",
     "scalar", colfmt(**{"Доставка за наш счёт": RUB}), "orders.paid_date_msk",
     description="Цена перевозчика по заказам с бесплатной для клиента доставкой.")
card("rev_daily", coll, "Выручка по дням",
     f"select paid_date_msk as \"День\", sum(total) as \"Выручка\", count(*) as \"Заказов\" {PAID} group by 1 order by 1",
     "bar", {"graph.dimensions": ["День"], "graph.metrics": ["Выручка"]}, "orders.paid_date_msk")
card("channels", coll, "Заказы по каналам",
     f"select channel_label as \"Канал\", count(*) as \"Заказов\", sum(total) as \"Выручка\" {PAID} group by 1 order by 3 desc",
     "row", {"graph.dimensions": ["Канал"], "graph.metrics": ["Выручка"]}, "orders.paid_date_msk")
card("carriers", coll, "Перевозчики",
     f"select coalesce(carrier, 'не указан') as \"Перевозчик\", count(*) as \"Заказов\" {PAID} group by 1 order by 2 desc",
     "pie", {"pie.dimension": "Перевозчик", "pie.metric": "Заказов"}, "orders.paid_date_msk")
card("statuses", coll, "Статусы заказов",
     "select status_label as \"Статус\", count(*) as \"Заказов\" from analytics.v_orders where true [[and {{dates}}]] "
     "group by 1 order by 2 desc",
     "row", {"graph.dimensions": ["Статус"], "graph.metrics": ["Заказов"]}, "orders.created_date_msk",
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
     "row", {"graph.dimensions": ["Сорт"], "graph.metrics": ["Выручка"]}, "items.created_date_msk")
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
    cid, has_period, has_dates = CARDS[key]
    maps = []
    if has_dates and "dates" in params:
        maps.append({"parameter_id": "dates", "card_id": cid,
                     "target": ["dimension", ["template-tag", "dates"], {"stage-number": 0}]})
    if has_period and "period" in params:
        maps.append({"parameter_id": "period", "card_id": cid, "target": ["variable", ["template-tag", "period_type"]]})
    d = {"id": _seq[0], "card_id": cid, "row": row, "col": col, "size_x": w, "size_y": h,
         "parameter_mappings": maps, "visualization_settings": {}}
    if tab is not None:
        d["dashboard_tab_id"] = tab
    return d


def make(name, desc, parameters, dashcards, tabs=None):
    did = call("POST", "/dashboard", {"name": name, "description": desc, "collection_id": coll})["id"]
    body = {"parameters": parameters, "dashcards": dashcards}
    if tabs:
        body["tabs"] = tabs
    call("PUT", f"/dashboard/{did}", body)
    print(f"{name}: {SITE}/dashboard/{did}")


# Один дашборд «Сводка»: вкладка «Обзор» — главное на одном экране (те же вопросы, что
# на остальных вкладках), дальше вкладки по темам. Фильтр «Период» — только у финансовых.
T = {"Обзор": -1, "Деньги": -2, "Воронка": -3, "Заказы": -4, "Удержание": -5, "Ассортимент": -6}
make("Сводка", "Деньги и продажи на одном дашборде. Выписка обновляется в 07:00 МСК, заказы — сразу. "
     "Без тестовых клиентов и заказов.", [P_PERIOD, P_DATES], [
    # Обзор
    dc("bal", 0, 0, 4, 3, T["Обзор"]), dc("runway", 0, 4, 4, 3, T["Обзор"]), dc("rev", 0, 8, 4, 3, T["Обзор"]),
    dc("ord_cnt", 0, 12, 4, 3, T["Обзор"]), dc("avg", 0, 16, 4, 3, T["Обзор"]), dc("conv", 0, 20, 4, 3, T["Обзор"]),
    dc("fcf_chart", 3, 0, 12, 6, T["Обзор"]), dc("rev_daily", 3, 12, 12, 6, T["Обзор"]),
    dc("funnel", 9, 0, 12, 7, T["Обзор"]), dc("costs", 9, 12, 12, 7, T["Обзор"]),
    # Деньги
    dc("bal", 0, 0, 6, 3, T["Деньги"]), dc("fcf", 0, 6, 6, 3, T["Деньги"]), dc("cfo", 0, 12, 6, 3, T["Деньги"]),
    dc("runway", 0, 18, 6, 3, T["Деньги"]),
    dc("fcf_chart", 3, 0, 12, 6, T["Деньги"]), dc("waterfall", 3, 12, 12, 6, T["Деньги"]),
    dc("costs", 9, 0, 12, 6, T["Деньги"]), dc("income", 9, 12, 12, 6, T["Деньги"]),
    dc("ddc", 15, 0, 24, 7, T["Деньги"]),
    dc("uncl_share", 22, 0, 6, 3, T["Деньги"]), dc("check", 25, 0, 6, 4, T["Деньги"]), dc("uncl", 22, 6, 18, 7, T["Деньги"]),
    # Воронка
    dc("funnel", 0, 0, 12, 8, T["Воронка"]), dc("conv", 0, 12, 6, 3, T["Воронка"]),
    dc("losses", 3, 12, 12, 5, T["Воронка"]), dc("daily", 8, 0, 24, 6, T["Воронка"]),
    dc("escal", 14, 0, 24, 5, T["Воронка"]),
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
], tabs=[{"id": v, "name": k} for k, v in T.items()])
