"""Коллекция «Финансы» в Metabase: пять дашбордов финмодели и формы ввода.

    MB_FIN_PW="$(cat /etc/teashop/metabase_fin_password)" python3 fin_build.py

Что делает:
- подключение «Финмодель (ввод)» — роль metabase_fin, видит только схему fin,
  на нём включены действия (Actions): через него работают формы;
- модели над таблицами ввода и по форме-действию на каждую;
- дашборды «Деньги», «Юнитка», «Реклама и CAC», «Склад и коды», «Ввод данных».
  Вопросы — SQL к представлениям fin через прежнее подключение (metabase_ro).

Повторный запуск: вопросы, модели и действия собираются заново в подколлекции
«Финансы — вопросы» (прежняя уходит в архив), дашборды обновляются на месте —
ссылки не меняются. Ручные правки дашбордов пропадут — правьте здесь.
«Сводку» (build.py) не трогает.
"""
import os
import time
import uuid

from mbapi import BASE, call

SITE = BASE[:-4]
RO_DB = 2  # прежнее подключение metabase_ro: чтение analytics, finance, fin
FIN_DB_NAME = "Финмодель (ввод)"
COLLECTION = "Финансы"
QUESTIONS = "Финансы — вопросы"
AUTHORS = ["Никита", "Илья"]
CATEGORIES = ["закупка", "логистика", "упаковка", "оборудование", "реклама", "подрядчик", "сервисы",
              "налоги_взносы", "банк", "маркировка", "прочее"]

RUB = {"suffix": " ₽", "decimals": 0}
PCT = {"number_style": "percent", "decimals": 1}
ALL_ROWS = {"graph.max_categories_enabled": False}


def colfmt(**cols):
    return {"column_settings": {f'["name","{name}"]': fmt for name, fmt in cols.items()}}


def find(items, **match):
    return next((i for i in items if all(i.get(k) == v for k, v in match.items())), None)


# --- подключение для форм -----------------------------------------------------------


def fin_database():
    """Подключение metabase_fin к той же базе, что metabase_ro, но только к схеме fin."""
    password = os.environ.get("MB_FIN_PW")
    if not password:
        raise SystemExit("Нет MB_FIN_PW: MB_FIN_PW=\"$(cat /etc/teashop/metabase_fin_password)\" python3 fin_build.py")
    ro = call("GET", f"/database/{RO_DB}")["details"]
    details = {k: ro[k] for k in ("host", "port", "dbname", "ssl", "ssl-mode", "ssl-use-client-auth", "tunnel-enabled")
               if k in ro}
    details.update({"user": "metabase_fin", "password": password,
                    "schema-filters-type": "inclusion", "schema-filters-patterns": "fin"})
    body = {"name": FIN_DB_NAME, "engine": "postgres", "details": details,
            "settings": {"database-enable-actions": True}}
    existing = find(call("GET", "/database")["data"], name=FIN_DB_NAME)
    if existing:
        call("PUT", f"/database/{existing['id']}", body)
        db = existing["id"]
    else:
        db = call("POST", "/database", body)["id"]
    # Действия включаются настройкой подключения; на случай, если POST её не принял.
    call("PUT", f"/database/{db}", {"settings": {"database-enable-actions": True}})
    call("POST", f"/database/{db}/sync_schema", {})
    for _ in range(40):
        tables = {t["name"]: t["id"] for t in call("GET", f"/database/{db}/metadata")["tables"] if t.get("schema") == "fin"}
        if {"expenses", "inputs", "ad_spend"} <= set(tables):
            return db, tables
        time.sleep(3)
    raise SystemExit("Metabase не видит таблиц fin через metabase_fin: схема применена (analytics/fin/apply.sh)? "
                     "Права выданы (grants.sql)?")


# --- коллекции ------------------------------------------------------------------------


def collections():
    every = call("GET", "/collection")
    root = find(every, name=COLLECTION, archived=False, location="/")
    if root is None:
        root = call("POST", "/collection", {"name": COLLECTION, "description": "Финмодель: деньги, юнитка, CAC, склад, ввод данных."})
    old = [c for c in every if c.get("name") == QUESTIONS and not c.get("archived")]
    questions = call("POST", "/collection", {"name": QUESTIONS, "parent_id": root["id"],
                                             "description": "Вопросы, модели и формы дашбордов «Финансы». Собирает fin_build.py."})
    return root["id"], questions["id"], old


# --- вопросы и формы ---------------------------------------------------------------------

CARDS = {}


def card(key, coll, name, sql, display, viz=None, description=None, db=RO_DB, model=False):
    body = {"name": name, "display": display, "visualization_settings": viz or {}, "collection_id": coll,
            "description": description,
            "dataset_query": {"type": "native", "database": db, "native": {"query": sql.strip(), "template-tags": {}}}}
    if model:
        body["type"] = "model"
    CARDS[key] = call("POST", "/card", body)["id"]
    return CARDS[key]


ACTIONS = {}


def field(name, title, kind, *, required=True, options=None, placeholder=None):
    """Поле формы: kind — date, number, text, select."""
    return {"name": name, "title": title, "kind": kind, "required": required, "options": options,
            "placeholder": placeholder}


def action(key, model_id, db, name, sql, fields, success):
    tag_type = {"date": "date", "number": "number", "text": "text", "select": "text", "textarea": "text"}
    param_type = {"date": "date/single", "number": "number/=", "text": "string/=", "select": "string/=",
                  "textarea": "string/="}
    tags, params, viz_fields = {}, [], {}
    for order, f in enumerate(fields):
        tags[f["name"]] = {"id": str(uuid.uuid4()), "name": f["name"], "display-name": f["title"],
                           "type": tag_type[f["kind"]], "required": f["required"]}
        params.append({"id": f["name"], "slug": f["name"], "name": f["title"], "type": param_type[f["kind"]],
                       "target": ["variable", ["template-tag", f["name"]]], "required": f["required"]})
        viz = {"id": f["name"], "order": order, "title": f["title"], "required": f["required"], "hidden": False,
               "fieldType": {"number": "number", "date": "date"}.get(f["kind"], "string"),
               "inputType": {"select": "select", "textarea": "text", "text": "string"}.get(f["kind"], f["kind"])}
        if f["options"]:
            viz["valueOptions"] = f["options"]
        if f["placeholder"]:
            viz["placeholder"] = f["placeholder"]
        viz_fields[f["name"]] = viz
    body = {"name": name, "type": "query", "model_id": model_id, "database_id": db,
            "dataset_query": {"type": "native", "database": db, "native": {"query": sql.strip(), "template-tags": tags}},
            "parameters": params,
            "visualization_settings": {"name": name, "type": "button", "fields": viz_fields,
                                       "submitButtonLabel": "Записать", "successMessage": success}}
    ACTIONS[key] = call("POST", "/action", body)["id"]
    return ACTIONS[key]


AUTHOR = field("entered_by", "Кто вносит", "select", options=AUTHORS)
NOTE = field("note", "Примечание", "textarea", required=False)
# Необязательные поля: без значения Metabase вырезает [[…]] целиком. Текст —
# nullif([[{{x}} ||]] '', ''), число — coalesce([[{{x}}, ]]по умолчанию).
OPT = "nullif([[{{%s}} ||]] '', '')"


def build_forms(qcoll, fin_db, tables):
    models = {}
    for table, title in (("expenses", "Расходы"), ("capital", "Вклады и выплаты партнёров"),
                         ("cash_snapshots", "Остатки по выписке"), ("purchase_lots", "Партии закупки"),
                         ("packing_runs", "Фасовка"), ("ad_spend", "Реклама по дням"), ("inputs", "Вводные (история)")):
        models[table] = card(f"model_{table}", qcoll, f"Ввод: {title}",
                             f"select * from fin.{table} order by created_at desc", "table", db=fin_db, model=True)
        if table != "inputs":
            # Исправить или удалить ошибочную строку — прямо в модели. Вводные — только дописываются.
            for kind, name in (("row/update", "Исправить"), ("row/delete", "Удалить")):
                call("POST", "/action", {"name": f"{name}: {title.lower()}", "type": "implicit", "kind": kind,
                                         "model_id": models[table]})
    return models


def input_keys(fin_db):
    result = call("POST", "/dataset", {"database": fin_db, "type": "native",
                                       "native": {"query": "select key, coalesce(label, key) from fin.inputs_current order by key"}})
    return [row[0] for row in result["data"]["rows"]]


def make_actions(models, fin_db):
    action("expense", models["expenses"], fin_db, "Добавить расход", f"""
insert into fin.expenses (date, category, amount, description, is_plan, paid_from, entered_by)
values ({{{{date}}}}::date, {{{{category}}}}, {{{{amount}}}}, {OPT % 'description'}, {{{{kind}}}} = 'план', {{{{paid_from}}}}, {{{{entered_by}}}})
""", [field("date", "Дата", "date"), field("category", "Категория", "select", options=CATEGORIES),
      field("amount", "Сумма, ₽", "number"), field("description", "Что купили", "text", required=False),
      field("kind", "Факт или план", "select", options=["факт", "план"]),
      field("paid_from", "Чем оплачено", "select", options=["счёт ИП", "мимо счёта"]), AUTHOR],
       "Расход записан. Оплаченный со счёта ИП в деньги второй раз не идёт — он уже в выписке.")
    action("capital", models["capital"], fin_db, "Вклад / выплата партнёра", f"""
insert into fin.capital (date, partner, amount, kind, note, entered_by)
values ({{{{date}}}}::date, {{{{partner}}}}, {{{{amount}}}}, {{{{kind}}}}, {OPT % 'note'}, {{{{entered_by}}}})
""", [field("date", "Дата", "date"), field("partner", "Партнёр", "select", options=AUTHORS),
      field("kind", "Вклад или выплата", "select", options=["вклад", "выплата"]),
      field("amount", "Сумма, ₽", "number"), NOTE, AUTHOR], "Записано.")
    action("snapshot", models["cash_snapshots"], fin_db, "Остаток по выписке", f"""
insert into fin.cash_snapshots (date, balance, source, note, entered_by)
values ({{{{date}}}}::date, {{{{balance}}}}, {OPT % 'source'}, {OPT % 'note'}, {{{{entered_by}}}})
""", [field("date", "На дату", "date"), field("balance", "Остаток, ₽", "number"),
      field("source", "Источник", "text", required=False, placeholder="Выписка ТБанк 14.10"), NOTE, AUTHOR],
       "Остаток записан — сверка на дашборде «Деньги».")
    action("lot", models["purchase_lots"], fin_db, "Партия закупки", f"""
insert into fin.purchase_lots (date, sku_name, supplier, qty_units, price_per_unit, discount, delivery_cost, note, entered_by)
values ({{{{date}}}}::date, {{{{sku_name}}}}, {OPT % 'supplier'}, {{{{qty_units}}}}, {{{{price_per_unit}}}},
        coalesce([[{{{{discount}}}} / 100.0, ]]0), coalesce([[{{{{delivery_cost}}}}, ]]0), {OPT % 'note'}, {{{{entered_by}}}})
""", [field("date", "Дата", "date"), field("sku_name", "SKU", "select", options=SKUS),
      field("supplier", "Поставщик", "text", required=False, placeholder="I-TEA"),
      field("qty_units", "Количество (кг или шт.)", "number"), field("price_per_unit", "Цена за кг или шт., ₽", "number"),
      field("discount", "Скидка, %", "number", required=False), field("delivery_cost", "Доставка партии, ₽", "number", required=False),
      NOTE, AUTHOR], "Партия записана — себестоимость пересчитана по партиям.")
    action("packing", models["packing_runs"], fin_db, "Фасовка", f"""
insert into fin.packing_runs (date, product, grams_in, packs_out, note, entered_by)
values ({{{{date}}}}::date, {{{{product}}}}, coalesce([[{{{{grams_in}}}}, ]]null::numeric), {{{{packs_out}}}}, {OPT % 'note'}, {{{{entered_by}}}})
""", [field("date", "Дата", "date"), field("product", "Что фасовали", "select", options=SKUS + BUNDLES),
      field("grams_in", "Взяли чая, г (у сета и набора — пусто)", "number", required=False),
      field("packs_out", "Получилось, шт.", "number"), NOTE, AUTHOR], "Фасовка записана.")
    action("ads", models["ad_spend"], fin_db, "Реклама за день", f"""
insert into fin.ad_spend (date, channel, spend_own, spend_bonus, impressions, clicks, note, entered_by)
values ({{{{date}}}}::date, {{{{channel}}}}, {{{{spend_own}}}}, coalesce([[{{{{spend_bonus}}}}, ]]0),
        coalesce([[{{{{impressions}}}}, ]]null::int), coalesce([[{{{{clicks}}}}, ]]null::int), {OPT % 'note'}, {{{{entered_by}}}})
""", [field("date", "Дата", "date"), field("channel", "Канал", "select", options=["VK Реклама", "Другое"]),
      field("spend_own", "Потрачено своих, ₽", "number"), field("spend_bonus", "Потрачено бонусов, ₽", "number", required=False),
      field("impressions", "Показы", "number", required=False), field("clicks", "Клики", "number", required=False),
      NOTE, AUTHOR], "Расход рекламы записан.")
    action("input", models["inputs"], fin_db, "Изменить вводную", f"""
insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select c.key, c.label,
       coalesce([[{{{{value}}}}, ]]null::numeric),
       {OPT % 'value_text'},
       c.unit, 'Изменено формой', {OPT % 'note'},
       coalesce([[{{{{valid_from}}}}::date, ]]current_date), {{{{entered_by}}}}
from fin.inputs_current c where c.key = {{{{key}}}}
""", [field("key", "Вводная", "select", options=KEYS),
      field("value", "Новое значение (число; доли: 3,4 % = 0.034)", "number", required=False),
      field("value_text", "Или текст / дата (2026-11-15)", "text", required=False),
      field("valid_from", "Действует с", "date", required=False), NOTE, AUTHOR],
       "Новая строка вводной записана, прежнее значение осталось в истории.")


# --- дашборды ------------------------------------------------------------------------------


def cards_money(q):
    card("m_status", q, "Деньги сейчас", """
select fin.fmt_rub(bank_balance) as "Остаток по выписке", bank_date as "На дату",
       fin.fmt_rub(holds_out) as "Холды банка (списания)", fin.fmt_rub(yookassa_in_transit) as "ЮKassa в пути",
       fin.fmt_rub(min_cash_reserve) as "Минимальный резерв"
from fin.v_cash_status""", "object")
    card("m_check", q, "Сверка с выпиской", """
select snapshot_date as "Снимок на", fin.fmt_rub(snapshot_balance) as "По снимку",
       fin.fmt_rub(snapshot_balance - snapshot_diff) as "По банку", fin.fmt_rub(snapshot_diff) as "Расхождение"
from fin.v_cash_status""", "object")
    card("m_limit", q, "Лимит пилота", """
select pilot_status as "Статус", fin.fmt_rub(pilot_loss) as "Накопленный убыток М1–М3",
       fin.fmt_rub(pilot_pause_loss) as "Пауза при", fin.fmt_rub(pilot_max_loss) as "Стоп новых трат при"
from fin.v_cash_status""", "object")
    card("m_budget_progress", q, "Известные расходы пилота против капитала",
         "select max(known_total) as known from fin.v_pilot_budget", "progress",
         {"progress.goal": 300000, **colfmt(known=RUB)})
    card("m_budget", q, "Бюджет пилота по статьям", """
select item as "Статья", plan_amount as "План", max_amount as "Максимум", source as "Источник"
from fin.v_pilot_budget order by line_no""", "table", colfmt(**{"План": RUB, "Максимум": RUB}))
    card("m_weekly", q, "Движение денег по неделям", """
select date_trunc('week', day)::date as week, coalesce(sum(inflow), 0) as inflow,
       -coalesce(sum(outflow) - coalesce(sum(transfers_out), 0), 0) as outflow, -coalesce(sum(transfers_out), 0) as transfers
from fin.v_cash group by 1 order by 1""", "bar",
         {"graph.dimensions": ["week"], "graph.metrics": ["inflow", "outflow", "transfers"], "stackable.stack_type": "stacked",
          **colfmt(inflow=RUB, outflow=RUB, transfers=RUB)},
         description="Приход, расход и переводы на свои счета (так оплачивают покупки с карты) по выписке.")
    card("m_balance", q, "Остаток по дням", "select day, balance from fin.v_cash order by day", "line",
         {"graph.dimensions": ["day"], "graph.metrics": ["balance"], **colfmt(balance=RUB)})


def cards_unit(q):
    card("u_margin", q, "Маржа до CAC: план и факт", """
select product as "Продукт", kind as "Тип", retail_price as "Цена", product_cost as "Себестоимость",
       plan_margin as "Маржа план", plan_margin_pct as "Маржа план, %", fact_margin_per_unit as "Маржа факт",
       units_sold as "Продано", fact_minus_plan as "Факт − план"
from fin.v_product_margin order by kind, plan_margin desc""", "table",
         colfmt(**{"Цена": RUB, "Себестоимость": RUB, "Маржа план": RUB, "Маржа факт": RUB, "Факт − план": RUB,
                   "Маржа план, %": PCT}))
    card("u_margin_chart", q, "Маржа до CAC на единицу, план", """
select product, margin_before_cac from fin.v_unit_cost order by margin_before_cac desc""", "row",
         {"graph.dimensions": ["product"], "graph.metrics": ["margin_before_cac"], **ALL_ROWS, **colfmt(margin_before_cac=RUB)})
    card("u_hist", q, "Заказы по марже до CAC", """
select (floor(margin_before_cac / 100) * 100)::int as margin_from, count(*) as orders
from fin.v_order_economics where margin_before_cac is not null group by 1 order by 1""", "bar",
         {"graph.dimensions": ["margin_from"], "graph.metrics": ["orders"], **colfmt(margin_from=RUB)})
    card("u_two", q, "Доля заказов от 2 пачек", """
select fin.fmt_pct(avg(two_plus_packs::int)) as share, count(*) as orders from fin.v_order_economics""", "object")
    card("u_orders", q, "Экономика заказов", """
select order_id as "Заказ", paid_date_msk as "Оплачен", is_first_order as "Первый", revenue as "Выручка",
       cogs_tea as "Чай", packaging_and_codes as "Упаковка и коды", carrier_cost as "Перевозчик",
       acquiring as "Эквайринг", usn_reserve as "УСН", api_cost as "Claude API", margin_before_cac as "Маржа до CAC",
       margin_pct as "Маржа, %", warning as "Внимание"
from fin.v_order_economics order by paid_date_msk desc, order_id desc""", "table",
         colfmt(**{k: RUB for k in ("Выручка", "Чай", "Упаковка и коды", "Перевозчик", "Эквайринг", "УСН",
                                    "Claude API", "Маржа до CAC")}, **{"Маржа, %": PCT}))
    card("u_pnl", q, "P&L по месяцам пилота", """
select month_label as "Месяц", period_start as "С", orders as "Заказы", new_clients as "Новые", revenue as "Выручка",
       cogs_tea as "Чай", packaging_and_codes as "Упаковка и коды", delivery_subsidy as "Субсидия доставки",
       returns as "Возвраты", acquiring as "Эквайринг", margin_before_cac as "Маржа до CAC", ads as "Реклама",
       contractor as "Подрядчик", other_opex as "Прочие", usn_reserve as "Резерв УСН",
       contributions_reserve as "Взносы", operating_profit as "Операционная прибыль", operating_margin as "Опер. маржа, %"
from fin.v_pnl_monthly order by month_no""", "table",
         colfmt(**{k: RUB for k in ("Выручка", "Чай", "Упаковка и коды", "Субсидия доставки", "Возвраты", "Эквайринг",
                                    "Маржа до CAC", "Реклама", "Подрядчик", "Прочие", "Резерв УСН", "Взносы",
                                    "Операционная прибыль")}, **{"Опер. маржа, %": PCT}))
    card("u_breakeven", q, "Заказов для безубыточности", """
select month_label as "Месяц", lead_product as "Lead-продукт", lead_margin as "Маржа lead",
       fixed_costs as "Постоянные", acquisition_ads as "Реклама", orders_to_break_even as "Заказов"
from fin.v_breakeven order by month_no""", "table", colfmt(**{"Маржа lead": RUB, "Постоянные": RUB, "Реклама": RUB}))


def cards_cac(q):
    card("c_monthly", q, "CAC по месяцам", """
select period_start as "Месяц", spend_own as "Реклама свои", spend_bonus as "Бонусы", clicks as "Клики",
       contractor as "Подрядчик", new_clients as "Новые клиенты", cac as "CAC",
       first_order_margin as "Маржа 1-го заказа", first_order_after_cac as "1-й заказ после CAC",
       after_cac_low as "После CAC 300", after_cac_mid as "После CAC 500", after_cac_high as "После CAC 700"
from fin.v_cac where period_type = 'месяц' order by period_start""", "table",
         colfmt(**{k: RUB for k in ("Реклама свои", "Бонусы", "Подрядчик", "CAC", "Маржа 1-го заказа", "1-й заказ после CAC",
                                    "После CAC 300", "После CAC 500", "После CAC 700")}))
    card("c_weekly", q, "Реклама по неделям: свои и бонусы", """
select period_start as week, coalesce(spend_own, 0) as own, coalesce(spend_bonus, 0) as bonus
from fin.v_cac where period_type = 'неделя' order by period_start""", "bar",
         {"graph.dimensions": ["week"], "graph.metrics": ["own", "bonus"], "stackable.stack_type": "stacked",
          **colfmt(own=RUB, bonus=RUB)})
    card("c_cac_line", q, "CAC и первый заказ после CAC по неделям", """
select period_start as week, cac, first_order_after_cac from fin.v_cac where period_type = 'неделя' order by period_start""",
         "line", {"graph.dimensions": ["week"], "graph.metrics": ["cac", "first_order_after_cac"],
                  **colfmt(cac=RUB, first_order_after_cac=RUB)})
    card("c_funnel", q, "Воронка бота по неделям", """
select period_start as week, coalesce(dialogs_started, 0) as dialogs, coalesce(drafts, 0) as drafts,
       coalesce(invoices, 0) as invoices, coalesce(first_paid, 0) as first_paid
from fin.v_cac where period_type = 'неделя' order by period_start""", "bar",
         {"graph.dimensions": ["week"], "graph.metrics": ["dialogs", "drafts", "invoices", "first_paid"]})


def cards_stock(q):
    card("s_stock", q, "Склад", """
select product as "Продукт", kind as "Тип", grams_bought as "Куплено, г", grams_used as "Израсходовано, г",
       grams_left as "Остаток, г", packed as "Расфасовано", sold as "Продано", refunded as "Возвращено",
       on_hand as "На руках", round(days_left) as "Хватит, дней",
       case when reorder then 'пора докупать' end as "Сигнал"
from fin.v_stock order by reorder desc, kind, product""", "table")
    card("s_reorder", q, "Пора докупать", """
select product as "Продукт", on_hand as "На руках", round(days_left) as "Хватит, дней"
from fin.v_stock where reorder order by days_left""", "table")
    card("s_codes", q, "Коды «Честного знака»", """
select gtin as "GTIN", imported as "Из СУЗ", used_in_orders as "В заказах", available as "Свободно",
       returned as "Вернулось", coalesce(printed::text, 'нет данных') as "Напечатано"
from fin.v_codes order by gtin""", "table")


def cards_entry(q):
    card("e_recent", q, "Последние 20 записей", """
select created_at as "Когда", entered_by as "Кто", form as "Форма", what as "Что", note as "Примечание"
from fin.v_recent_entries""", "table")
    card("e_inputs", q, "Вводные сейчас", """
select key as "Ключ", label as "Вводная", coalesce(value::text, value_text, 'нет данных') as "Значение", unit as "Ед.",
       valid_from as "С", entered_by as "Кто", status as "Статус"
from fin.inputs_current order by key""", "table")


_seq = [0]


def dc(key, row, col, w, h):
    _seq[0] -= 1
    return {"id": _seq[0], "card_id": CARDS[key], "row": row, "col": col, "size_x": w, "size_y": h,
            "parameter_mappings": [], "visualization_settings": {}}


def button(key, label, row, col, w=6, h=2):
    _seq[0] -= 1
    return {"id": _seq[0], "card_id": None, "action_id": ACTIONS[key], "row": row, "col": col, "size_x": w, "size_y": h,
            "parameter_mappings": [],
            "visualization_settings": {"button.label": label, "actionDisplayType": "button",
                                       "virtual_card": {"display": "action", "visualization_settings": {},
                                                        "dataset_query": {}, "archived": False, "name": None}}}


def dashboard(root, name, desc, dashcards):
    existing = find(call("GET", f"/collection/{root}/items?models=dashboard")["data"], name=name)
    if existing:
        did = existing["id"]
        call("PUT", f"/dashboard/{did}", {"name": name, "description": desc})
    else:
        did = call("POST", "/dashboard", {"name": name, "description": desc, "collection_id": root})["id"]
    call("PUT", f"/dashboard/{did}", {"dashcards": dashcards})
    print(f"{name}: {SITE}/dashboard/{did}" + (" (обновлён)" if existing else " (новый)"))
    return did


def verify():
    """Каждый вопрос выполняется: ошибка — в вывод, а не в дашборд на глазах у владельцев."""
    bad = 0
    for key, cid in CARDS.items():
        result = call("POST", f"/card/{cid}/query", {})
        if result.get("status") != "completed" or result.get("error"):
            bad += 1
            print(f"  ✗ {key}: {str(result.get('error'))[:200]}")
    print("Вопросы: все выполняются" if not bad else f"Вопросов с ошибкой: {bad}")
    return bad


if __name__ == "__main__":
    fin_db, tables = fin_database()
    root, qcoll, old = collections()
    rows = call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {"query":
                "select 's', sku_name from fin.sku where active union all select 'b', bundle from fin.bundles where active"}})
    SKUS = [r[1] for r in rows["data"]["rows"] if r[0] == "s"]
    BUNDLES = [r[1] for r in rows["data"]["rows"] if r[0] == "b"]
    KEYS = input_keys(fin_db)
    models = build_forms(qcoll, fin_db, tables)
    make_actions(models, fin_db)
    cards_money(qcoll)
    cards_unit(qcoll)
    cards_cac(qcoll)
    cards_stock(qcoll)
    cards_entry(qcoll)

    dashboard(root, "Деньги", "Остаток по выписке и сверка, движение по неделям, известные расходы пилота против 300 000 ₽, "
              "статус лимита пилота. Выписка — 07:00 и 21:00 МСК.", [
        dc("m_status", 0, 0, 12, 4), dc("m_limit", 0, 12, 12, 4),
        dc("m_check", 4, 0, 12, 4), dc("m_budget_progress", 4, 12, 12, 4),
        dc("m_balance", 8, 0, 12, 6), dc("m_weekly", 8, 12, 12, 6),
        dc("m_budget", 14, 0, 24, 8)])
    dashboard(root, "Юнитка", "Маржа до CAC по продуктам — план и факт, заказы по марже, доля заказов от 2 пачек, P&L и "
              "безубыточность.", [
        dc("u_two", 0, 0, 6, 3), dc("u_hist", 0, 6, 18, 6), dc("u_margin_chart", 6, 0, 12, 10),
        dc("u_breakeven", 6, 12, 12, 5), dc("u_margin", 16, 0, 24, 8), dc("u_pnl", 24, 0, 24, 7),
        dc("u_orders", 31, 0, 24, 8)])
    dashboard(root, "Реклама и CAC", "Расход своими и бонусами, клики, новые клиенты, CAC, первый заказ после CAC, "
              "воронка бота. Реклама вносится формой «Реклама за день».", [
        dc("c_weekly", 0, 0, 12, 6), dc("c_cac_line", 0, 12, 12, 6), dc("c_funnel", 6, 0, 24, 6),
        dc("c_monthly", 12, 0, 24, 7)])
    dashboard(root, "Склад и коды", "Остатки чая, пачек, сетов и кодов ЧЗ; «пора докупать» — пачек меньше чем на 14 дней.", [
        dc("s_reorder", 0, 0, 10, 6), dc("s_codes", 0, 10, 14, 6), dc("s_stock", 6, 0, 24, 9)])
    dashboard(root, "Ввод данных", "Формы финмодели. Каждая запись — с автором; вводные не перезаписываются, а дописываются.", [
        button("expense", "Добавить расход", 0, 0), button("capital", "Вклад / выплата партнёра", 0, 6),
        button("snapshot", "Остаток по выписке", 0, 12), button("lot", "Партия закупки", 0, 18),
        button("packing", "Фасовка", 2, 0), button("ads", "Реклама за день", 2, 6),
        button("input", "Изменить вводную", 2, 12),
        dc("e_recent", 4, 0, 24, 8), dc("e_inputs", 12, 0, 24, 10)])

    failed = verify()
    for c in old:
        call("PUT", f"/collection/{c['id']}", {"archived": True})
    if old:
        print(f"Прежние вопросы в архиве: {len(old)} коллекц.")
    raise SystemExit(1 if failed else 0)
