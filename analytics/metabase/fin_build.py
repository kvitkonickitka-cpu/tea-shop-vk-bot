"""Коллекция «Финансы» в Metabase: дашборд «Финансы» (пять вкладок) и формы ввода.

    MB_FIN_PW="$(cat /etc/teashop/metabase_fin_password)" python3 fin_build.py

Что делает:
- подключение «Финмодель (ввод)» — роль metabase_fin, видит только схему fin,
  на нём включены действия (Actions): через него работают формы;
- модели над таблицами ввода и по форме-действию на каждую;
- дашборд «Финансы», вкладки «Деньги», «Юнитка», «Реклама и CAC», «Склад и коды»,
  «Ввод данных».
  Вопросы — SQL к представлениям fin через прежнее подключение (metabase_ro).

Повторный запуск: вопросы, модели и действия собираются заново в подколлекции
«Финансы — вопросы» (прежняя уходит в архив), дашборд обновляется на месте —
ссылка не меняется. Ручные правки дашбордов пропадут — правьте здесь.
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

# Пробел между тысячами, как в «32 731 ₽», а не «32,731 ₽».
RUB = {"suffix": " ₽", "decimals": 0, "number_separators": ", "}
PCT = {"number_style": "percent", "decimals": 1}
ALL_ROWS = {"graph.max_categories_enabled": False}


def colfmt(**cols):
    return {"column_settings": {f'["name","{name}"]': fmt for name, fmt in cols.items()}}


def find(items, **match):
    return next((i for i in items if all(i.get(k) == v for k, v in match.items())), None)


# --- подключение для форм -----------------------------------------------------------


# Партии закупки с 014 — shop.lots (форма на «Пульс → Склад»); «Фасовка» убрана:
# фасуем под заказ, списание пишет страница сборки.
FORM_TABLES = ("expenses", "capital", "cash_snapshots", "ad_spend", "inputs",
               "carrier_acts", "supply_lead_times", "tax_calendar", "code_batches", "code_writeoffs")
# Каталог бота (схема shop): правят формы «Пульс» → «Каталог». В словаре
# таблиц — с именем схемы, чтобы не спутать с таблицами fin.
SHOP_TABLES = ("shop.catalog", "shop.bundle_items", "shop.lots", "shop.stock_moves")


def form_tables(db):
    """Таблицы подключения форм: fin — по имени, shop — «shop.имя»."""
    return {(t["name"] if t.get("schema") == "fin" else f"{t['schema']}.{t['name']}"): t["id"]
            for t in call("GET", f"/database/{db}/metadata")["tables"] if t.get("schema") in ("fin", "shop")}


def fin_database():
    """Подключение metabase_fin к той же базе, что metabase_ro, но только к схемам fin и shop."""
    password = os.environ.get("MB_FIN_PW")
    if not password:
        raise SystemExit("Нет MB_FIN_PW: MB_FIN_PW=\"$(cat /etc/teashop/metabase_fin_password)\" python3 fin_build.py")
    ro = call("GET", f"/database/{RO_DB}")["details"]
    details = {k: ro[k] for k in ("host", "port", "dbname", "ssl", "ssl-mode", "ssl-use-client-auth", "tunnel-enabled")
               if k in ro}
    details.update({"user": "metabase_fin", "password": password,
                    "schema-filters-type": "inclusion", "schema-filters-patterns": "fin,shop"})
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
        tables = form_tables(db)
        # Ждём все таблицы форм: после новой миграции Metabase видит их не сразу,
        # и сборка падала на KeyError посреди работы (21.10.2026, supply_lead_times).
        if set(FORM_TABLES) | set(SHOP_TABLES) <= set(tables):
            return db, tables
        time.sleep(3)
    raise SystemExit("Metabase не видит таблиц fin и shop через metabase_fin: схема fin применена (analytics/fin/apply.sh)? "
                     "Права выданы (grants.sql)? Бот с каталогом в базе задеплоен (схема shop)?")


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
                         ("cash_snapshots", "Остатки по выписке"),
                         ("ad_spend", "Реклама по дням"), ("inputs", "Вводные (история)"),
                         ("carrier_acts", "Акты перевозчиков"), ("supply_lead_times", "Сроки поставки"),
                         ("tax_calendar", "Налоговый календарь"), ("code_batches", "Партии кодов ЧЗ"),
                         ("code_writeoffs", "Списанные коды ЧЗ")):
        # Модель прямо на таблице, без SQL: только такой Metabase даёт действия
        # «Исправить» и «Удалить» (row/update, row/delete).
        body = {"name": f"Ввод: {title}", "display": "table", "visualization_settings": {}, "collection_id": qcoll,
                "type": "model", "dataset_query": {"type": "query", "database": fin_db,
                                                   "query": {"source-table": tables[table]}}}
        models[table] = CARDS[f"model_{table}"] = call("POST", "/card", body)["id"]
        if table != "inputs":
            # Исправить или удалить ошибочную строку — прямо в модели. Вводные — только дописываются;
            # налоговый календарь — только отмечается (оплачено, подано), не удаляется.
            for kind, name in (("row/update", "Исправить"), ("row/delete", "Удалить")):
                if table in ("tax_calendar", "code_batches") and kind == "row/delete":
                    continue
                call("POST", "/action", {"name": f"{name}: {title.lower()}", "type": "implicit", "kind": kind,
                                         "model_id": models[table]})
    return models


# Название вводной в списке формы: подпись из модели («Карты: эквайринг без НДС»),
# а не ключ. Если подписи совпадают — к ней дописывается ключ. То же выражение
# стоит в SQL формы «Изменить вводную»: по нему форма находит ключ.
INPUT_TITLE = ("case when count(*) over (partition by coalesce(label, key)) > 1 "
               "then coalesce(label, key) || ' · ' || key else coalesce(label, key) end")


def input_keys(fin_db):
    result = call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {"query":
                  f"select {INPUT_TITLE} as title from fin.inputs_current order by 1"}})
    return [row[0] for row in result["data"]["rows"]]


# Открытый плановый расход в списке формы: «№ · дата · сумма · что». В SQL — номер до точки.
PLAN_TITLE = ("p.id || ' · ' || to_char(p.plan_date, 'DD.MM.YYYY') || ' · ' || fin.fmt_rub(p.amount) "
              "|| ' · ' || left(p.what, 40)")
PLAN_ID = "nullif(split_part([[{{%s}} ||]] '', ' · ', 1), '')::bigint"


def open_plans(fin_db):
    """Плановые расходы, которые ещё не закрыты фактом, — для поля «Закрывает план»."""
    result = call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {"query":
                  f"select {PLAN_TITLE} from fin.v_plans p where not p.closed order by p.plan_date, p.id"}})
    return [row[0] for row in result["data"]["rows"]]


def budget_items(fin_db):
    result = call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {"query":
                  "select item from fin.budget_articles where item <> 'Не сопоставлено' order by sort"}})
    return [row[0] for row in result["data"]["rows"]]


def make_actions(models, fin_db):
    plans, items = open_plans(fin_db), budget_items(fin_db)
    # Статья — своя, если выбрана; иначе — по категории и описанию. Факт, оплативший
    # плановый расход, закрывает его: план уходит из прогноза, деньги — по факту.
    action("expense", models["expenses"], fin_db, "Добавить расход", f"""
insert into fin.expenses (date, category, amount, description, is_plan, paid_from, budget_item, closes_plan_id, entered_by)
values ({{{{date}}}}::date, {{{{category}}}}, {{{{amount}}}}, {OPT % 'description'}, {{{{kind}}}} = 'план', {{{{paid_from}}}},
        {OPT % 'item'}, {PLAN_ID % 'closes'}, {{{{entered_by}}}})
""", [field("date", "Дата", "date"), field("category", "Категория", "select", options=CATEGORIES),
      field("amount", "Сумма, ₽", "number"), field("description", "Что купили", "text", required=False),
      field("kind", "Факт или план", "select", options=["факт", "план"]),
      field("paid_from", "Чем оплачено", "select", options=["счёт ИП", "мимо счёта"]),
      field("item", "Статья бюджета (пусто — по категории)", "select", required=False, options=items),
      field("closes", "Закрывает план (факт оплаты планового расхода)", "select", required=False, options=plans),
      AUTHOR],
       "Расход записан. Оплаченный со счёта ИП в деньги второй раз не идёт — он уже в выписке. "
       "План — в прогнозе на свою дату, пока его не закроет факт.")
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
    action("ads", models["ad_spend"], fin_db, "Реклама за день", f"""
insert into fin.ad_spend (date, channel, spend_own, spend_bonus, impressions, clicks, note, entered_by)
values ({{{{date}}}}::date, {{{{channel}}}}, {{{{spend_own}}}}, coalesce([[{{{{spend_bonus}}}}, ]]0),
        coalesce([[{{{{impressions}}}}, ]]null::int), coalesce([[{{{{clicks}}}}, ]]null::int), {OPT % 'note'}, {{{{entered_by}}}})
""", [field("date", "Дата", "date"), field("channel", "Канал", "select", options=["VK Реклама", "Другое"]),
      field("spend_own", "Потрачено своих, ₽", "number"), field("spend_bonus", "Потрачено бонусов, ₽", "number", required=False),
      field("impressions", "Показы", "number", required=False), field("clicks", "Клики", "number", required=False),
      NOTE, AUTHOR], "Расход рекламы записан.")
    action("input", models["inputs"], fin_db, "Изменить вводную", f"""
with c as (select *, {INPUT_TITLE} as title from fin.inputs_current)
insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select c.key, c.label,
       coalesce([[{{{{value}}}}, ]]null::numeric),
       {OPT % 'value_text'},
       c.unit, 'Изменено формой', {OPT % 'note'},
       coalesce([[{{{{valid_from}}}}::date, ]]current_date), {{{{entered_by}}}}
from c where c.title = {{{{key}}}}
""", [field("key", "Вводная", "select", options=KEYS),
      field("value", "Новое значение (число; доли: 3,4 % = 0.034)", "number", required=False),
      field("value_text", "Или текст / дата (2026-11-15)", "text", required=False),
      field("valid_from", "Действует с", "date", required=False), NOTE, AUTHOR],
       "Новая строка вводной записана, прежнее значение осталось в истории.")
    action("act", models["carrier_acts"], fin_db, "Акт перевозчика", f"""
insert into fin.carrier_acts (carrier, track, amount, kind, act_date, act_number, entered_by)
values ({{{{carrier}}}}, {{{{track}}}}, {{{{amount}}}}, {{{{kind}}}}, coalesce([[{{{{act_date}}}}::date, ]]null::date),
        {OPT % 'act_number'}, {{{{entered_by}}}})
""", [field("carrier", "Перевозчик", "select", options=["ozon", "cdek"]),
      field("track", "Трек-номер (отправление Ozon или накладная СДЭК)", "text"),
      field("amount", "Сумма по акту, ₽", "number"),
      field("kind", "Что", "select", options=["доставка", "возврат"]),
      field("act_date", "Дата акта", "date", required=False), field("act_number", "Номер акта", "text", required=False),
      AUTHOR], "Строка акта записана — сверка с котировкой на вкладке «Юнитка». Много строк — CSV командой /act в чате Ops.")
    action("writeoff", models["code_writeoffs"], fin_db, "Списать код ЧЗ", f"""
insert into fin.code_writeoffs (gtin, serial, reason, note, entered_by)
values ({{{{gtin}}}}, {{{{serial}}}}, {{{{reason}}}}, {OPT % 'note'}, {{{{entered_by}}}})
""", [field("gtin", "GTIN", "text"), field("serial", "Серийный номер", "text"),
      field("reason", "Причина", "select", options=["испорчена этикетка", "брак печати", "утеря", "другое"]),
      NOTE, AUTHOR], "Код списан — в заказ он попасть не должен.")
    action("lead", models["supply_lead_times"], fin_db, "Срок поставки", f"""
insert into fin.supply_lead_times (scope, name, lead_days, valid_from, note, entered_by)
values ({{{{scope}}}}, {{{{name}}}}, {{{{lead_days}}}}, coalesce([[{{{{valid_from}}}}::date, ]]current_date), {OPT % 'note'}, {{{{entered_by}}}})
""", [field("scope", "Для чего", "select", options=["поставщик", "sku"]),
      field("name", "Поставщик или SKU", "text", placeholder="I-TEA"),
      field("lead_days", "Срок поставки, дней", "number"), field("valid_from", "Действует с", "date", required=False),
      NOTE, AUTHOR], "Срок записан — точка заказа на вкладке «Склад и коды».")


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
         "select max(known_total) as \"Известные расходы\" from fin.v_pilot_budget", "progress",
         {"progress.goal": 300000, **colfmt(**{"Известные расходы": RUB})})
    card("m_budget", q, "Бюджет пилота по статьям", """
select item as "Статья", plan_amount as "План", max_amount as "Максимум", source as "Источник"
from fin.v_pilot_budget order by line_no""", "table", colfmt(**{"План": RUB, "Максимум": RUB}))
    card("m_weekly", q, "Движение денег по неделям", """
select date_trunc('week', day)::date as "Неделя", coalesce(sum(inflow), 0) as "Приход",
       -coalesce(sum(outflow) - coalesce(sum(transfers_out), 0), 0) as "Расход",
       -coalesce(sum(transfers_out), 0) as "Переводы на свои счета"
from fin.v_cash where day >= fin.inp_date('ip_registration_date') group by 1 order by 1""", "bar",
         {"graph.dimensions": ["Неделя"], "graph.metrics": ["Приход", "Расход", "Переводы на свои счета"],
          "stackable.stack_type": "stacked", **colfmt(**{"Приход": RUB, "Расход": RUB, "Переводы на свои счета": RUB})},
         description="Приход, расход и переводы на свои счета (так оплачивают покупки с карты) по выписке.")
    card("m_balance", q, "Остаток по дням", """
select day as "День", balance as "Остаток" from fin.v_cash where day >= fin.inp_date('ip_registration_date') order by day""",
         "line", {"graph.dimensions": ["День"], "graph.metrics": ["Остаток"], **colfmt(**{"Остаток": RUB})})


def cards_pilot(q):
    card("m_gates", q, "Ворота пилота", """
select gate as "Ворота", check_name as "Проверка",
       case when unit = '%' then fin.fmt_pct(value) else fin.fmt_rub(value) end as "Сейчас",
       case when unit = '%' then fin.fmt_pct(threshold) else fin.fmt_rub(threshold) end as "Порог",
       case status when 'зелёный' then '🟢 зелёный' when 'красный' then '🔴 красный' else '⚪ нет данных' end as "Статус",
       period_end as "Срок", case when period_complete then 'итог' else 'идёт' end as "Период"
from fin.v_pilot_gates order by sort""", "table",
         description="Неделя 2: конверсия платного диалога в оплату. Неделя 6: платный CAC и маржа первого заказа после CAC. "
                     "М3: повторы за 45 дней, маржа 90 дней − CAC, прибыль М3. Пороги — вводные gate_*.")
    card("m_forecast", q, "Деньги на 8 недель", """
select week_start as "Неделя с", opening as "На начало", payouts_in as "ЮKassa", -planned_expenses as "План расходов",
       -ads as "Реклама", -taxes as "Налоги и взносы", -(bank_fee + services) as "Банк и сервисы",
       closing as "На конец", lowest as "Минимум за неделю",
       case when below_reserve then 'ниже резерва' end as "Резерв"
from fin.v_cash_forecast order by week_no""", "table",
         colfmt(**{k: RUB for k in ("На начало", "ЮKassa", "План расходов", "Реклама", "Налоги и взносы",
                                    "Банк и сервисы", "На конец", "Минимум за неделю")}),
         description="Остаток по выписке минус холды, плюс выплаты ЮKassa в пути, минус плановые расходы, реклама по "
                     "дневному бюджету, налоги и взносы по сроку (налоговый календарь), банк с декабря, сервисы.")
    card("m_forecast_line", q, "Прогноз остатка и резерв", """
select day as "День", closing as "Остаток", fin.inp('min_cash_reserve') as "Резерв" from fin.v_cash_forecast_daily order by day""",
         "line", {"graph.dimensions": ["День"], "graph.metrics": ["Остаток", "Резерв"],
                  **colfmt(**{"Остаток": RUB, "Резерв": RUB})})
    card("m_breach", q, "Когда остаток уйдёт ниже резерва", """
select coalesce(to_char(max(reserve_breach_date), 'DD.MM.YYYY'), 'не в ближайшие 8 недель') as "Дата",
       fin.fmt_rub(max(min_cash_reserve)) as "Резерв",
       case when bool_or(ads_plan_missing) then 'дневной бюджет рекламы не задан — реклама не учтена' end as "Внимание"
from fin.v_cash_forecast""", "object")
    card("m_taxes", q, "Налоговый календарь", """
select due_date as "Срок", event as "Событие", fin.fmt_rub(amount_hint) as "Ориентир", status as "Статус",
       fin.fmt_rub(paid_amount) as "Уплачено", done_on as "Когда", note as "Примечание"
from fin.tax_calendar order by due_date, event""", "table",
         description="Отметка «оплачено» или «подано» — командой /tax в чате Ops или в модели «Ввод: Налоговый календарь».")
    card("m_kudir", q, "КУДиР: доходы", """
select income_date as "Дата дохода", doc_number as "Документ", doc_date as "Дата документа", content as "Содержание",
       income as "Доход, ₽" from fin.v_kudir order by income_date, doc_date, doc_number""", "table",
         colfmt(**{"Доход, ₽": {"decimals": 2}}),
         description="Скачать — кнопкой выгрузки (xlsx/csv) или /kudir 2026-10 в чате Ops. Дата дохода — по правилу "
                     "KUDIR_INCOME_DATE; правило подтвердить у бухгалтера.")


def cards_unit(q):
    card("u_margin", q, "Маржа до CAC: план и факт", """
select product as "Продукт", kind as "Тип", retail_price as "Цена", product_cost as "Себестоимость",
       plan_margin as "Маржа план", plan_margin_pct as "Маржа план, %", fact_margin_per_unit as "Маржа факт",
       units_sold as "Продано", fact_minus_plan as "Факт − план"
from fin.v_product_margin order by kind, plan_margin desc""", "table",
         colfmt(**{"Цена": RUB, "Себестоимость": RUB, "Маржа план": RUB, "Маржа факт": RUB, "Факт − план": RUB,
                   "Маржа план, %": PCT}))
    card("u_margin_chart", q, "Маржа до CAC на единицу, план", """
select product as "Продукт", margin_before_cac as "Маржа до CAC" from fin.v_unit_cost order by margin_before_cac desc""", "row",
         {"graph.dimensions": ["Продукт"], "graph.metrics": ["Маржа до CAC"], **ALL_ROWS, **colfmt(**{"Маржа до CAC": RUB})})
    card("u_hist", q, "Заказы по марже до CAC", """
select (floor(margin_before_cac / 100) * 100)::int as "Маржа от", count(*) as "Заказов"
from fin.v_order_economics where margin_before_cac is not null group by 1 order by 1""", "bar",
         {"graph.dimensions": ["Маржа от"], "graph.metrics": ["Заказов"], **colfmt(**{"Маржа от": RUB})})
    card("u_two", q, "Доля заказов от 2 пачек", """
select fin.fmt_pct(avg(two_plus_packs::int)) as "Доля от 2 пачек", count(*) as "Оплаченных заказов"
from fin.v_order_economics""", "object")
    card("u_orders", q, "Экономика заказов", """
select order_id as "Заказ", paid_date_msk as "Оплачен", is_first_order as "Первый", revenue as "Выручка",
       cogs_tea as "Чай", packaging_and_codes as "Упаковка и коды", carrier_cost as "Перевозчик",
       acquiring as "Эквайринг", usn_reserve as "УСН", api_cost as "Claude API", margin_before_cac as "Маржа до CAC",
       margin_pct as "Маржа, %", warning as "Внимание", acquiring_source as "Эквайринг — откуда",
       carrier_cost_source as "Доставка — откуда", carrier_return_cost as "Обратная доставка"
from fin.v_order_economics order by paid_date_msk desc, order_id desc""", "table",
         colfmt(**{k: RUB for k in ("Выручка", "Чай", "Упаковка и коды", "Перевозчик", "Эквайринг", "УСН",
                                    "Claude API", "Маржа до CAC")}, **{"Маржа, %": PCT}))
    card("u_pnl", q, "P&L по месяцам пилота", """
select month_label as "Месяц", period_start as "С", orders as "Заказы", new_clients as "Новые", revenue as "Выручка",
       cogs_tea as "Чай", packaging_and_codes as "Упаковка и коды", delivery_subsidy as "Субсидия доставки",
       returns as "Возвраты", acquiring as "Эквайринг", margin_before_cac as "Маржа до CAC", ads as "Реклама",
       contractor as "Подрядчик", other_opex as "Прочие", usn_reserve as "Резерв УСН",
       operating_profit as "Операционная прибыль", operating_margin as "Опер. маржа, %",
       usn_income as "Доход УСН (КУДиР)", contributions_paid as "Взносы уплачены (деньги)",
       contributions_reserve as "Взносы год/12 (справочно)"
from fin.v_pnl_monthly order by month_no""", "table",
         colfmt(**{k: RUB for k in ("Выручка", "Чай", "Упаковка и коды", "Субсидия доставки", "Возвраты", "Эквайринг",
                                    "Маржа до CAC", "Реклама", "Подрядчик", "Прочие", "Резерв УСН",
                                    "Операционная прибыль", "Доход УСН (КУДиР)", "Взносы уплачены (деньги)",
                                    "Взносы год/12 (справочно)")}, **{"Опер. маржа, %": PCT}),
         description="Резерв УСН = прирост max(0; 6 % × доходы КУДиР с начала года − уплаченные взносы). Взносы в прибыль "
                     "не входят: это деньги (прогноз на 8 недель), а налог уже уменьшен на уплаченные.")
    card("u_breakeven", q, "Заказов для безубыточности", """
select month_label as "Месяц", margin_basis as "Маржа — по чему", margin_used as "Маржа",
       fixed_costs as "Постоянные", acquisition_ads as "Реклама", orders_to_break_even as "Заказов"
from fin.v_breakeven order by month_no""", "table", colfmt(**{"Маржа": RUB, "Постоянные": RUB, "Реклама": RUB}),
         description="Фактическая средняя маржа заказа до CAC за 14 дней, если оплат ≥ 10; иначе — маржа lead-продукта.")
    card("u_carrier", q, "Доставка: котировка и факт", """
select month as "Месяц", carrier as "Перевозчик", orders as "Заказов с фактом", quote as "Котировки", actual as "Факт",
       returns as "Обратная доставка", fin.fmt_pct(deviation) as "Отклонение",
       case when over_limit then 'больше допуска' end as "Внимание"
from fin.v_carrier_check_monthly order by month desc, carrier""", "table",
         colfmt(**{"Котировки": RUB, "Факт": RUB, "Обратная доставка": RUB}),
         description="Факт — акт перевозчика (форма или /act) или API СДЭКа. Отклонение больше carrier_deviation_max — ⚠️ в Ops.")


def cards_cac(q):
    card("c_monthly", q, "CAC по месяцам", """
select period_start as "Месяц", spend_own as "Реклама свои", spend_bonus as "Бонусы", clicks as "Клики",
       contractor as "Подрядчик", new_clients as "Новые клиенты", cac as "CAC",
       first_order_margin as "Маржа 1-го заказа", first_order_after_cac as "1-й заказ после CAC",
       new_clients_ad as "Новые из рекламы", cac_paid as "Платный CAC",
       first_order_after_cac_paid as "1-й заказ после платного CAC",
       after_cac_low as "После CAC 300", after_cac_mid as "После CAC 500", after_cac_high as "После CAC 700"
from fin.v_cac where period_type = 'месяц' order by period_start""", "table",
         colfmt(**{k: RUB for k in ("Реклама свои", "Бонусы", "Подрядчик", "CAC", "Маржа 1-го заказа", "1-й заказ после CAC",
                                    "Платный CAC", "1-й заказ после платного CAC",
                                    "После CAC 300", "После CAC 500", "После CAC 700")}),
         description="CAC — общий (реклама и подрядчик на всех новых клиентов). Платный CAC — реклама своими на новых "
                     "клиентов, пришедших по рекламной ссылке (first touch ad:*).")
    card("c_weekly", q, "Реклама по неделям: свои и бонусы", """
select period_start as "Неделя", coalesce(spend_own, 0) as "Свои", coalesce(spend_bonus, 0) as "Бонусы"
from fin.v_cac where period_type = 'неделя' order by period_start""", "bar",
         {"graph.dimensions": ["Неделя"], "graph.metrics": ["Свои", "Бонусы"], "stackable.stack_type": "stacked",
          **colfmt(**{"Свои": RUB, "Бонусы": RUB})})
    card("c_cac_line", q, "CAC и первый заказ после CAC по неделям", """
select period_start as "Неделя", cac as "CAC", first_order_after_cac as "1-й заказ после CAC"
from fin.v_cac where period_type = 'неделя' order by period_start""",
         "line", {"graph.dimensions": ["Неделя"], "graph.metrics": ["CAC", "1-й заказ после CAC"],
                  **colfmt(**{"CAC": RUB, "1-й заказ после CAC": RUB})})
    card("c_funnel", q, "Воронка бота по неделям", """
select period_start as "Неделя", coalesce(dialogs_started, 0) as "Диалоги", coalesce(drafts, 0) as "Черновики",
       coalesce(invoices, 0) as "Счета", coalesce(first_paid, 0) as "Первые оплаты"
from fin.v_cac where period_type = 'неделя' order by period_start""", "bar",
         {"graph.dimensions": ["Неделя"], "graph.metrics": ["Диалоги", "Черновики", "Счета", "Первые оплаты"]})


def cards_referral(q):
    card("c_referral", q, "Рекомендации друзей: скидки и CAC канала", """
select period_start as "Месяц", new_clients_referral as "Новые по коду друга", referral_discounts as "Скидки им",
       cac_referral as "CAC канала", all_discounts as "Все скидки по кодам"
from fin.v_referral_cac where period_type = 'месяц' order by period_start""", "table",
         colfmt(**{"Скидки им": RUB, "CAC канала": RUB, "Все скидки по кодам": RUB}),
         description="Скидка уже вычтена из выручки заказа (цены позиций); здесь — сколько она стоила на нового клиента.")


def cards_stock(q):
    card("s_stock", q, "Склад", """
select product as "Продукт", kind as "Тип", grams_bought as "Куплено, г", grams_used as "Израсходовано, г",
       grams_left as "Остаток, г", packed as "Расфасовано", sold as "Продано", refunded as "Возвращено",
       on_hand as "На руках", round(days_left) as "Хватит, дней",
       case when order_now then 'пора заказывать' when reorder then 'мало' end as "Сигнал",
       lead_days as "Поставка, дн.", round(reorder_point) as "Точка заказа",
       case when not_in_bot then 'нет в каталоге бота' end as "Бот"
from fin.v_stock order by order_now desc nulls last, reorder desc, kind, product""", "table",
         description="Точка заказа = (срок поставки + фасовка и маркировка + страховой запас) × продажи в день за 14 дней. "
                     "Пусто — сроки не заданы (вводные и форма «Срок поставки»).")
    card("s_reorder", q, "Пора заказывать", """
select product as "Продукт", on_hand as "На руках", round(reorder_point) as "Точка заказа", round(days_left) as "Хватит, дней"
from fin.v_stock where order_now or (order_now is null and reorder) order by days_left""", "table")
    card("s_codes", q, "Коды «Честного знака»", """
select gtin as "GTIN", imported as "Из СУЗ", used_in_orders as "В заказах", available as "Свободно",
       returned as "Вернулось", printed as "В партиях", submitted as "Подано", in_circulation as "В обороте",
       written_off as "Списано", sold_not_in_circulation as "В заказах без ввода"
from fin.v_codes order by gtin""", "table",
         description="Ввод в оборот — партиями: /codes <GTIN> <кол-во> в чате Ops даёт файл для ЛК ЧЗ, "
                     "статус — /circulation или модель «Ввод: Партии кодов ЧЗ».")
    card("s_batches", q, "Партии кодов: ввод в оборот", """
select id as "Партия", created_at as "Заведена", gtin as "GTIN", product as "Товар", codes_count as "Кодов",
       status as "Статус", submitted_at as "Подана", accepted_at as "Принята", used_in_orders as "В заказах"
from fin.v_code_batches""", "table")


def cards_entry(q):
    card("e_recent", q, "Последние 20 записей", """
select created_at as "Когда", entered_by as "Кто", form as "Форма", what as "Что", note as "Примечание"
from fin.v_recent_entries""", "table")
    card("e_inputs", q, "Вводные сейчас", """
select label as "Вводная", coalesce(value::text, value_text, 'нет данных') as "Значение", unit as "Ед.",
       valid_from as "С", entered_by as "Кто", status as "Статус", key as "Ключ"
from fin.inputs_current order by label""", "table")


_seq = [0]


# Один дашборд «Финансы», по вкладке на тему — как «Сводка». Отрицательные id
# вкладок — новые; Metabase заменит их своими.
TABS = {"Деньги": -1, "Юнитка": -2, "Реклама и CAC": -3, "Склад и коды": -4, "Ввод данных": -5}
DASHBOARD = "Финансы"
# Дашборды до объединения (20.10.2026): «Деньги» становится «Финансами» — его
# ссылка /dashboard/6 продолжает работать, — остальные уходят в архив.
OLD_DASHBOARDS = ("Деньги", "Юнитка", "Реклама и CAC", "Склад и коды", "Ввод данных")


def dc(key, tab, row, col, w, h):
    _seq[0] -= 1
    return {"id": _seq[0], "card_id": CARDS[key], "dashboard_tab_id": TABS[tab], "row": row, "col": col,
            "size_x": w, "size_y": h, "parameter_mappings": [], "visualization_settings": {}}


def button(key, label, row, col, w=6, h=2):
    _seq[0] -= 1
    return {"id": _seq[0], "card_id": None, "action_id": ACTIONS[key], "dashboard_tab_id": TABS["Ввод данных"],
            "row": row, "col": col, "size_x": w, "size_y": h, "parameter_mappings": [],
            "visualization_settings": {"button.label": label, "actionDisplayType": "button",
                                       "virtual_card": {"display": "action", "visualization_settings": {},
                                                        "dataset_query": {}, "archived": False, "name": None}}}


def dashboard(root, desc, dashcards):
    items = call("GET", f"/collection/{root}/items?models=dashboard")["data"]
    existing = find(items, name=DASHBOARD) or find(items, name=OLD_DASHBOARDS[0])
    if existing:
        did = existing["id"]
        call("PUT", f"/dashboard/{did}", {"name": DASHBOARD, "description": desc})
    else:
        did = call("POST", "/dashboard", {"name": DASHBOARD, "description": desc, "collection_id": root})["id"]
    call("PUT", f"/dashboard/{did}", {"dashcards": dashcards,
                                      "tabs": [{"id": tid, "name": name} for name, tid in TABS.items()]})
    for item in items:
        if item["name"] in OLD_DASHBOARDS and item["id"] != did:
            call("PUT", f"/dashboard/{item['id']}", {"archived": True})
            print(f"  в архив: {item['name']} (/dashboard/{item['id']})")
    print(f"{DASHBOARD}: {SITE}/dashboard/{did}" + (" (обновлён)" if existing else " (новый)"))
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
    # С 21.10.2026 «Финансы» заменены «Пульсом» и «Служебным» (dash_build.py, формы — там же);
    # пересборка вернула бы архивный дашборд.
    if any(d.get("name") == "Пульс" for d in call("GET", "/dashboard/")):
        raise SystemExit("«Финансы» заменены «Пульсом» и «Служебным» — собирайте их: dash_build.py.")
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
    cards_pilot(qcoll)
    cards_unit(qcoll)
    cards_cac(qcoll)
    cards_referral(qcoll)
    cards_stock(qcoll)
    cards_entry(qcoll)

    dashboard(root, "Финмодель: деньги по выписке, юнитка, реклама и CAC, склад и коды, ввод данных формами. "
              "Выписка — 07:00 и 21:00 МСК, заказы — сразу.", [
        # Деньги
        dc("m_gates", "Деньги", 0, 0, 24, 5),
        dc("m_status", "Деньги", 5, 0, 12, 6), dc("m_limit", "Деньги", 5, 12, 12, 6),
        dc("m_check", "Деньги", 11, 0, 12, 5), dc("m_budget_progress", "Деньги", 11, 12, 12, 5),
        dc("m_balance", "Деньги", 16, 0, 12, 6), dc("m_weekly", "Деньги", 16, 12, 12, 6),
        dc("m_breach", "Деньги", 22, 0, 8, 4), dc("m_forecast_line", "Деньги", 22, 8, 16, 6),
        dc("m_forecast", "Деньги", 28, 0, 24, 6),
        dc("m_taxes", "Деньги", 34, 0, 24, 6), dc("m_kudir", "Деньги", 40, 0, 24, 7),
        dc("m_budget", "Деньги", 47, 0, 24, 9),
        # Юнитка
        dc("u_two", "Юнитка", 0, 0, 6, 4), dc("u_hist", "Юнитка", 0, 6, 18, 6),
        dc("u_margin_chart", "Юнитка", 6, 0, 12, 10), dc("u_breakeven", "Юнитка", 6, 12, 12, 5),
        dc("u_margin", "Юнитка", 16, 0, 24, 8), dc("u_pnl", "Юнитка", 24, 0, 24, 7),
        dc("u_orders", "Юнитка", 31, 0, 24, 8), dc("u_carrier", "Юнитка", 39, 0, 24, 5),
        # Реклама и CAC
        dc("c_weekly", "Реклама и CAC", 0, 0, 12, 6), dc("c_cac_line", "Реклама и CAC", 0, 12, 12, 6),
        dc("c_funnel", "Реклама и CAC", 6, 0, 24, 6), dc("c_monthly", "Реклама и CAC", 12, 0, 24, 7),
        dc("c_referral", "Реклама и CAC", 19, 0, 24, 5),
        # Склад и коды
        dc("s_reorder", "Склад и коды", 0, 0, 10, 6), dc("s_codes", "Склад и коды", 0, 10, 14, 6),
        dc("s_stock", "Склад и коды", 6, 0, 24, 9), dc("s_batches", "Склад и коды", 15, 0, 24, 6),
        # Ввод данных
        button("expense", "Добавить расход", 0, 0), button("capital", "Вклад / выплата партнёра", 0, 6),
        button("snapshot", "Остаток по выписке", 0, 12), button("lot", "Партия закупки", 0, 18),
        button("packing", "Фасовка", 2, 0), button("ads", "Реклама за день", 2, 6),
        button("input", "Изменить вводную", 2, 12), button("act", "Акт перевозчика", 2, 18),
        button("lead", "Срок поставки", 4, 0), button("writeoff", "Списать код ЧЗ", 4, 6),
        dc("e_recent", "Ввод данных", 6, 0, 24, 8), dc("e_inputs", "Ввод данных", 14, 0, 24, 10)])

    failed = verify()
    for c in old:
        call("PUT", f"/collection/{c['id']}", {"archived": True})
    if old:
        print(f"Прежние вопросы в архиве: {len(old)} коллекц.")
    raise SystemExit(1 if failed else 0)
