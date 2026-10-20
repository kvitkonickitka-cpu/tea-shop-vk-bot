"""Дашборды «Пульс» (каждый день) и «Служебное» (раз в неделю, в конце месяца) в коллекции «Дашборды».

    MB_FIN_PW="$(cat /etc/teashop/metabase_fin_password)" python3 dash_build.py

Один экран — один вопрос, ответ — в верхнем ряду. Все числа — из представлений fin
(миграция 009) через подключение metabase_ro; формы ввода — через «Финмодель (ввод)»
(роль metabase_fin), как в fin_build.py, функции которого здесь и используются.

Что делает:
- глобальные форматы Metabase: дата 20.10.2026, время 14:37, «32 731 ₽», «12,5 %»;
- вопросы, модели и формы — заново в подколлекции «Пульс и Служебное — вопросы»
  (прежняя уходит в архив в конце, когда дашборды уже на новых вопросах);
- дашборды «Пульс» и «Служебное» обновляются на месте: ссылки не меняются;
- «Пульс» — главная страница Metabase, оба дашборда закреплены в «Дашбордах».

Ручные правки дашбордов пропадут — правьте здесь.
"""
import uuid

import fin_build as fb
from mbapi import BASE, call

SITE = BASE[:-4]
RO_DB = fb.RO_DB
ROOT = "Дашборды"
QUESTIONS = "Пульс и Служебное — вопросы"
PULSE, SERVICE = "Пульс", "Служебное"
PULSE_TABS = ["Сегодня", "Деньги", "Продажи", "Товар"]
SERVICE_TABS = ["Ввод данных", "Сверки", "Отчёты", "Вводные"]

# Рубли целые, проценты с одним знаком; разделители — из глобальных настроек
# (пробел для тысяч, запятая для дробей). Компактные «33k» выключены везде.
RUB = {"suffix": " ₽", "decimals": 0}
PCT = {"number_style": "percent", "decimals": 1}
FULL = {"graph.label_value_formatting": "full"}
# Цвета — только для статуса.
GREEN, YELLOW, RED, GREY = "#84BB4C", "#F2A86F", "#ED6E6E", "#A7ADB4"
# Месяц подписью «окт 2026»: у SQL-вопроса Metabase пишет месяц датой первого числа.
RU_MONTH = ("(array['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'])"
            "[extract(month from {0})::int] || ' ' || extract(year from {0})::int")


def month_label(expr):
    return RU_MONTH.format(expr)


def colfmt(**cols):
    return {"column_settings": {f'["name","{name}"]': fmt for name, fmt in cols.items()}}


def merge(*dicts):
    out = {}
    for d in dicts:
        for k, v in d.items():
            if k == "column_settings" and k in out:
                out[k] = {**out[k], **v}
            else:
                out[k] = v
    return out


# --- глобальные настройки -------------------------------------------------------------


def set_formatting():
    """U5: даты 20.10.2026, время 24 ч, «32 731 ₽», «12,5 %». Только форматы, админ-настройки не трогаем."""
    call("PUT", "/setting/custom-formatting", {"value": {
        "type/Temporal": {"date_style": "D/M/YYYY", "date_separator": ".", "date_abbreviate": False,
                          "time_style": "HH:mm"},
        "type/Number": {"number_separators": ", "},
        "type/Currency": {"currency": "RUB", "currency_style": "symbol", "currency_in_header": True},
    }})


# --- коллекции и дашборды -------------------------------------------------------------


def collections():
    every = call("GET", "/collection")
    roots = [c for c in every if c.get("name") == ROOT and c.get("location") == "/" and not c.get("archived")]
    root = max(roots, key=lambda c: c["id"]) if roots else call("POST", "/collection", {"name": ROOT})
    old = [c for c in every if c.get("name") == QUESTIONS and not c.get("archived")]
    qcoll = call("POST", "/collection", {"name": QUESTIONS, "parent_id": root["id"],
                                         "description": "Вопросы, модели и формы «Пульса» и «Служебного». Собирает dash_build.py."})
    return root["id"], qcoll["id"], old


def ensure_dashboard(root, name, desc, tabs):
    """Дашборд на месте (та же ссылка), на всю ширину, с вкладками; вернуть id и id вкладок по имени."""
    items = call("GET", f"/collection/{root}/items?models=dashboard")["data"]
    existing = fb.find(items, name=name)
    did = existing["id"] if existing else call("POST", "/dashboard", {"name": name, "collection_id": root})["id"]
    call("PUT", f"/dashboard/{did}", {"description": desc, "width": "full"})
    current = {t["name"]: t["id"] for t in call("GET", f"/dashboard/{did}").get("tabs", [])}
    body = [{"id": current.get(t, -(i + 1)), "name": t} for i, t in enumerate(tabs)]
    # Вкладки сначала без карточек: id вкладок нужны ссылкам «Требует внимания».
    call("PUT", f"/dashboard/{did}", {"tabs": body, "dashcards": []})
    ids = {t["name"]: t["id"] for t in call("GET", f"/dashboard/{did}")["tabs"]}
    return did, ids


# --- вопросы --------------------------------------------------------------------------

CARDS = {}


def card(key, coll, name, sql, display, viz=None, description=None):
    body = {"name": name, "display": display, "collection_id": coll, "description": description,
            "visualization_settings": merge(FULL, viz or {}),
            "dataset_query": {"type": "native", "database": RO_DB, "native": {"query": sql.strip(), "template-tags": {}}}}
    CARDS[key] = call("POST", "/card", body)["id"]
    return CARDS[key]


def tile(key, coll, name, sql, description):
    """Плитка: одно текстовое значение со светофором (🟢 / 🟡 / 🔴) — не сжимается в «33k»."""
    return card(key, coll, name, sql, "scalar", {"scalar.compact_primary_number": False}, description)


def one_value(sql):
    rows = call("POST", "/dataset", {"database": RO_DB, "type": "native", "native": {"query": sql}})["data"]["rows"]
    return rows[0][0] if rows else None


def cards_today(q, links):
    tile("t_free", q, "Свободные деньги", """
select case when free_cash < 0 then '🔴 ' when free_cash < reserve then '🟡 ' else '🟢 ' end || fin.fmt_rub(free_cash)
from fin.v_money_now""",
         "Остаток по выписке банка минус минимальный резерв (вводная «Минимальный денежный резерв»). "
         "Выписка обновляется в 07:00 и 21:00 МСК. 🟢 — свободно больше резерва, 🟡 — меньше, 🔴 — ниже резерва.")
    tile("t_runway", q, "Денег хватит до", """
select case when runout_date is null then '🟢 дольше ' || to_char(horizon, 'DD.MM.YYYY')
            when runout_date < pilot_end then '🔴 ' || to_char(runout_date, 'DD.MM.YYYY')
            else '🟡 ' || to_char(runout_date, 'DD.MM.YYYY') end
from fin.v_money_now""",
         "Первый день, когда прогноз остатка опускается ниже резерва. Прогноз: остаток − холды + оставшиеся вклады "
         "по плану + выплаты ЮKassa в пути − оставшийся бюджет пилота по статьям (план − потрачено) − реклама по "
         "плану М1–М3 − сервисы, банк, подрядчик, налоги и взносы по сроку. 🔴 — раньше конца пилота.")
    tile("t_capital", q, "Вклады партнёров", """
select case when contributed >= plan then '🟢 ' else '🟡 ' end || replace(fin.fmt_rub(contributed), ' ₽', '')
       || ' из ' || fin.fmt_rub(plan)
from fin.v_capital_status""",
         "Внесено: «Взнос собственника» по выписке и форма «Вклад / выплата партнёра» (кто внёс). План — вводная "
         "«Стартовый капитал». По партнёрам — вкладка «Деньги».")
    tile("t_pilot", q, "Пилот", """
with s as (select * from fin.v_pilot_status),
plan as (select orders_needed, period_start from fin.v_breakeven_plan
         where period_start <= current_date order by period_start desc limit 1),
fact as (select count(*) as n from analytics.v_orders
         where is_paid and not is_refunded and paid_date_msk > current_date - 7)
select case when not s.started
            then '⚪ Не начался — старт ' || to_char(s.start_date, 'DD.MM') || ' (через ' || s.days_to_start || ' дн.)'
            else case when f.n >= round(p.orders_needed * 7 / 30.0) then '🟢 '
                      when f.n >= round(p.orders_needed * 7 / 30.0) / 2 then '🟡 ' else '🔴 ' end
                 || f.n || ' заказов за 7 дней из ' || round(p.orders_needed * 7 / 30.0) end
from s cross join fact f left join plan p on true""",
         "До старта — сколько дней осталось. После — оплаченные заказы за 7 дней против плана: заказов для "
         "безубыточности текущего месяца × 7 / 30. Убыток пилота против паузы и стопа — на вкладке «Деньги».")
    url = links
    card("t_attention", q, "Требует внимания", f"""
select level as "!", text as "Что", case place when 'money' then 'Деньги' when 'entry' then 'Ввод данных'
            when 'checks' then 'Сверки' when 'sales' then 'Продажи' end as "Где",
       case place when 'money' then '{url["money"]}' when 'entry' then '{url["entry"]}'
            when 'checks' then '{url["checks"]}' when 'sales' then '{url["sales"]}' end as "Ссылка"
from fin.v_attention order by case level when '🔴' then 0 when '🟡' then 1 else 2 end, sort""", "table",
         {"table.columns": [{"name": "!", "enabled": True}, {"name": "Что", "enabled": True},
                            {"name": "Где", "enabled": True}, {"name": "Ссылка", "enabled": False}],
          **colfmt(**{"Где": {"view_as": "link", "link_text": "{{Где}}", "link_url": "{{Ссылка}}"}})},
         "Проверки: вклады против бюджета до старта, покупки с карты без записи «мимо счёта» (дольше 3 дней), "
         "партия чая за 21 день до старта, «Не разобрано» в выписке, сверка остатка, Claude не учтён ботом > 20 % "
         "за 7 дней, чеки и выплаты ЮKassa; после старта — заказы не у перевозчика и вопросы менеджеру.")
    card("t_balance", q, "Остаток и прогноз", """
select day as "День", actual as "Остаток", forecast as "Прогноз", reserve as "Резерв" from fin.v_balance_chart order by day""",
         "line", {"graph.dimensions": ["День"], "graph.metrics": ["Остаток", "Прогноз", "Резерв"],
                  "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Деньги, ₽",
                  "line.marker_enabled": False, "graph.y_axis.unpin_from_zero": False,
                  "series_settings": {"Остаток": {"color": "#509EE3"},
                                      "Прогноз": {"color": "#509EE3", "line.style": "dashed"},
                                      "Резерв": {"color": RED, "line.style": "dotted"}},
                  **colfmt(Остаток=RUB, Прогноз=RUB, Резерв=RUB)},
         "Сплошная — остаток по выписке на конец дня, пунктир — прогноз до конца пилота + 30 дней (как в «Денег "
         "хватит до»), точки — минимальный резерв.")
    card("t_soon", q, "Продажи", """
select 'Продажи, воронка и CAC появятся здесь после ' || to_char(fin.inp_date('pilot_start_date'), 'DD.MM')
where not exists (select 1 from analytics.v_orders where is_paid)""", "scalar",
         {"scalar.compact_primary_number": False}, "Карточка пропадает, как только появится первый оплаченный заказ.")


def cards_money(q):
    card("m_spend", q, "Куда ушли деньги по месяцам", f"""
select {month_label("date_trunc('month', s.day)")} as "Месяц", s.budget_item as "Статья", round(sum(s.amount)) as "Расход"
from fin.v_spend s join fin.budget_articles b on b.item = s.budget_item
group by date_trunc('month', s.day), s.budget_item, b.sort order by date_trunc('month', s.day), b.sort""", "bar",
         {"graph.dimensions": ["Месяц", "Статья"], "graph.metrics": ["Расход"], "stackable.stack_type": "stacked",
          "graph.show_values": True, "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Расход, ₽",
          "series_settings": SERIES_COLORS, **colfmt(Расход=RUB)},
         "Расходы бизнеса: выписка (без переводов себе) + покупки «мимо счёта» по статьям бюджета пилота. "
         "Переводы себе на покупки сюда не входят — их сверка на «Служебном → Сверки». Оборудование — тоже здесь, "
         "в ДДС оно во «Вложениях».")
    card("m_partners", q, "Кто сколько вложил", """
select partner as "Партнёр", plan as "План", contributed as "Внесено", greatest(coalesce(plan, 0) - contributed, 0) as "Осталось",
       contributed / nullif(plan, 0) as "Внесено, %"
from fin.v_partner_capital order by sort""", "table",
         colfmt(**{"План": RUB, "Внесено": RUB, "Осталось": RUB, "Внесено, %": {**PCT, "show_mini_bar": True}}),
         "План — вводные «Вклад Никиты» и «Вклад Ильи». Внесено — по форме «Вклад / выплата партнёра»; взносы по "
         "выписке, которые формой ещё не размечены, — строкой «Не указано, чей».")
    card("m_budget", q, "Бюджет пилота: сколько осталось", """
select item as "Статья", plan as "План", spent as "Потрачено", left_amount as "Осталось", spent_share as "Потрачено, %"
from fin.v_pilot_budget_status order by sort""", "table",
         merge(colfmt(**{"План": RUB, "Потрачено": RUB, "Осталось": RUB, "Потрачено, %": {**PCT, "show_mini_bar": True}}),
               {"table.column_formatting": [{"columns": ["Осталось"], "type": "single", "operator": "<",
                                             "value": 0, "color": RED, "highlight_row": False}]}),
         "План — статьи бюджета пилота из вводных; потрачено — выписка и «мимо счёта», сведённые к статьям "
         "(fin.budget_article_map). «Не сопоставлено» — траты без статьи: разметить формой «Разметить операцию».")
    card("m_forecast", q, "Деньги на 8 недель вперёд", """
select week_start as "Неделя с", round(opening) as "На начало", round(inflow) as "Придёт", round(outflow) as "Уйдёт",
       round(closing) as "На конец", case when below_reserve then '🔴 ниже резерва' else '🟢' end as "Резерв"
from fin.v_weekly_forecast order by week_no""", "table",
         colfmt(**{"На начало": RUB, "Придёт": RUB, "Уйдёт": RUB, "На конец": RUB}),
         "Тот же прогноз, что «Денег хватит до»: придёт — вклады по плану и выплаты ЮKassa; уйдёт — бюджет пилота, "
         "реклама по плану, сервисы, банк, подрядчик, налоги.")
    card("m_loss", q, "Убыток пилота", """
select case when pilot_loss >= pilot_max_loss then '🔴 ' when pilot_loss >= pilot_pause_loss then '🟡 ' else '🟢 ' end
       || fin.fmt_rub(pilot_loss) || ' — пауза при ' || fin.fmt_rub(pilot_pause_loss) || ', стоп при ' || fin.fmt_rub(pilot_max_loss)
from fin.v_pilot_status where started and pilot_loss is not null""", "scalar",
         {"scalar.compact_primary_number": False},
         "Накопленный убыток М1–М3 по P&L против вводных «Убыток для паузы» и «Максимальный убыток». До старта "
         "пилота карточки нет.")


def cards_sales(q):
    paid = "exists (select 1 from analytics.v_orders where is_paid)"
    card("s_funnel", q, "Воронка: от диалога до вручения", f"""
with e as (
  select client_key, case when event = 'dialog_start' then 1 when event = 'draft_created' then 2
    when event like 'invoice\\_%' and event not in ('invoice_expired', 'invoice_canceled') then 3
    when event = 'payment_succeeded' then 4 when event = 'delivered' then 5 end as step
  from analytics.v_funnel_events
  where client_key is not null and created_date_msk >= fin.inp_date('pilot_start_date'))
select case step when 1 then 'Диалог' when 2 then 'Черновик' when 3 then 'Счёт' when 4 then 'Оплата' else 'Вручение' end
       as "Шаг", count(distinct client_key) as "Клиентов"
from e where step is not null and {paid} group by step order by step""", "funnel",
         {"funnel.dimension": "Шаг", "funnel.metric": "Клиентов", "funnel.type": "funnel"},
         "Сколько разных клиентов с начала пилота дошли до каждого шага; проценты — переход от первого шага.")
    card("s_daily", q, "Заказы по дням против плана", f"""
with d as (select d::date as day from generate_series(fin.inp_date('pilot_start_date'), current_date, interval '1 day') d),
o as (select paid_date_msk as day, count(*) as n, sum(total) as rev from analytics.v_orders
      where is_paid and not is_refunded group by 1)
select d.day as "День", coalesce(o.n, 0) as "Заказов",
       (select b.orders_needed / 30.0 from fin.v_breakeven_plan b where b.period_start <= d.day
        order by b.period_start desc limit 1) as "План",
       coalesce(o.rev, 0) as "Выручка"
from d left join o using (day) where {paid} order by 1""", "combo",
         {"graph.dimensions": ["День"], "graph.metrics": ["Заказов", "План"],
          "series_settings": {"Заказов": {"display": "bar", "color": "#509EE3"},
                              "План": {"display": "line", "color": GREEN, "line.style": "dashed"}},
          "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Заказов в день", "graph.show_values": True},
         "Оплаченные заказы без возвратов по дню оплаты. План — заказов для безубыточности месяца / 30.")
    card("s_cac", q, "CAC по неделям: цель и лимит", """
select c.period_start as "Неделя", round(c.cac_paid) as "CAC", t.goal as "Цель", t.limit_value as "Лимит"
from fin.v_cac c cross join fin.v_kpi_targets t
where t.metric = 'cac' and c.period_type = 'неделя' and c.period_start >= date_trunc('week', fin.inp_date('pilot_start_date'))
  and c.cac_paid is not null order by 1""", "line",
         {"graph.dimensions": ["Неделя"], "graph.metrics": ["CAC", "Цель", "Лимит"],
          "series_settings": {"CAC": {"color": "#509EE3"}, "Цель": {"color": GREEN, "line.style": "dashed"},
                              "Лимит": {"color": RED, "line.style": "dashed"}},
          "graph.show_values": True, "graph.x_axis.title_text": "", "graph.y_axis.title_text": "CAC, ₽",
          **colfmt(CAC=RUB, Цель=RUB, Лимит=RUB)},
         "Реклама своими деньгами за неделю / новые клиенты, пришедшие по рекламной ссылке. Бонусы ВК не входят. "
         "Цель и лимит — вводные «Цель: CAC» и «Лимит: CAC».")
    card("s_check", q, "Средний чек по неделям", """
select date_trunc('week', o.paid_date_msk)::date as "Неделя", round(avg(o.total)) as "Средний чек", t.goal as "Цель"
from analytics.v_orders o cross join fin.v_kpi_targets t
where t.metric = 'avg_check' and o.is_paid and not o.is_refunded group by 1, t.goal order by 1""", "line",
         {"graph.dimensions": ["Неделя"], "graph.metrics": ["Средний чек", "Цель"],
          "series_settings": {"Средний чек": {"color": "#509EE3"}, "Цель": {"color": GREEN, "line.style": "dashed"}},
          "graph.show_values": True, "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Средний чек, ₽",
          **colfmt(**{"Средний чек": RUB, "Цель": RUB})},
         "Оплаченные заказы без возвратов, с доставкой. Цель — вводная «Цель: средний чек» (цена «Точки входа»).")


def cards_product(q):
    lead = one_value("select value_text from fin.inputs_current where key = 'lead_product'") or "Набор «Точка входа»"
    api = one_value("select round(fin.api_per_new_buyer()) from fin.inputs limit 1")
    card("p_waterfall", q, f"Из чего складывается маржа «{lead.replace('Набор ', '').strip('«»')}»", f"""
select step as "Шаг", round(amount) as "Сумма" from fin.v_unit_waterfall
where product = (select value_text from fin.inputs_current where key = 'lead_product') order by step_no""", "waterfall",
         {"graph.dimensions": ["Шаг"], "graph.metrics": ["Сумма"], "waterfall.show_total": True,
          "waterfall.increase_color": GREEN, "waterfall.decrease_color": RED, "waterfall.total_color": "#509EE3",
          "graph.show_values": True, "graph.x_axis.axis_enabled": True, "graph.x_axis.title_text": "",
          "graph.y_axis.title_text": "На один набор, ₽", **colfmt(Сумма=RUB)},
         f"Маржа до рекламы = цена × (1 − УСН − эквайринг с чеком и НДС) − чай − упаковка и коды − доставка и прочее "
         f"на заказ − Claude. Claude в себестоимости — {api if api is not None else 'нет данных'} ₽ на нового "
         f"покупателя: API на консультацию × курс / конверсия. Источник — fin.v_unit_cost, сегодняшние вводные.")
    card("p_margin", q, "Маржа до рекламы по товарам", """
select name || case kind_group when 'Пачки' then ' · пачка' when 'Блины' then ' · блин' else '' end
            || case when sold_by_bot then ' · бот' else '' end as "Товар",
       case when not low_margin then round(margin_before_cac) end as "Маржа",
       case when low_margin then round(margin_before_cac) end as "Ниже порога"
from fin.v_product_card order by kind_sort, margin_before_cac desc""", "row",
         {"graph.dimensions": ["Товар"], "graph.metrics": ["Маржа", "Ниже порога"], "stackable.stack_type": "stacked",
          "graph.max_categories_enabled": False, "graph.show_values": True,
          "series_settings": {"Маржа": {"color": GREEN}, "Ниже порога": {"color": RED}},
          "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Маржа на единицу, ₽", **colfmt(Маржа=RUB, **{"Ниже порога": RUB})},
         "Сверху вниз: наборы, сеты, пачки, блины. Красным — маржа до рекламы ниже порога (вводная «Порог низкой "
         "маржи», 20 % цены). «бот» — товар есть в каталоге бота. Одно определение маржи — fin.v_unit_cost.")
    card("p_breakeven_m1", q, "Сколько заказов нужно в месяц", """
select string_agg(month_label || ': ' || orders_needed, ' · ' order by month_no) from fin.v_breakeven_plan""",
         "scalar", {"scalar.compact_primary_number": False},
         "Заказов «Точки входа» в месяц, чтобы покрыть постоянные расходы (сервисы, банк, взносы / 12, подрядчик) "
         "и рекламу по плану (вводные «Реклама по плану: М1…М3», свои деньги, без бонусов).")
    card("p_breakeven", q, "Безубыточность по месяцам пилота", """
select month_label as "Месяц", round(fixed_costs) as "Постоянные", ads_plan as "Реклама по плану",
       round(lead_margin) as "Маржа набора", orders_needed as "Нужно заказов"
from fin.v_breakeven_plan order by month_no""", "table",
         colfmt(**{"Постоянные": RUB, "Реклама по плану": RUB, "Маржа набора": RUB}),
         "Нужно заказов = ⌈(постоянные + реклама по плану) / маржа до рекламы «Точки входа»⌉.")
    card("p_scenarios", q, "Сколько заказов нужно при другой рекламе", """
select ads as "Реклама в месяц",
       max(orders_needed) filter (where month_no = 1) as "М1", max(orders_needed) filter (where month_no = 2) as "М2",
       max(orders_needed) filter (where month_no = 3) as "М3"
from fin.v_breakeven_scenarios group by ads order by ads""", "table", colfmt(**{"Реклама в месяц": RUB}),
         "Сценарии рекламы — вводная «Сценарии рекламы в месяц» (через запятую).")
    card("p_sales", q, "Продажи по товарам", """
select coalesce(n.display_name, l.fin_product, l.item_name) as "Товар", sum(l.quantity) as "Продано",
       count(distinct l.order_id) as "Заказов",
       count(distinct l.order_id) filter (where not e.is_first_order) as "Из них повторных"
from fin.v_order_lines l join fin.v_order_economics e using (order_id)
left join fin.product_names n on n.product = l.fin_product
where not l.is_refunded group by 1 order by 2 desc""", "table", None,
         "Оплаченные заказы без возвратов. Повторный — не первый оплаченный заказ клиента.")


def cards_entry(q):
    card("e_recent", q, "Последние 20 записей", """
select created_at as "Когда", entered_by as "Кто", form as "Форма", what as "Что", note as "Примечание"
from fin.v_recent_entries""", "table", None, "Всё, что внесли формами, — сверху новое.")


def cards_checks(q):
    card("c_snapshot", q, "Остаток: снимок против банка", """
select snapshot_date as "Снимок на", snapshot_balance as "По снимку", snapshot_balance - snapshot_diff as "По банку",
       snapshot_diff as "Расхождение" from fin.v_cash_status""", "table",
         colfmt(**{"По снимку": RUB, "По банку": RUB, "Расхождение": RUB}),
         "Последний снимок формы «Остаток по выписке» против остатка, который cashflow собрал из выписки.")
    card("c_card", q, "Покупки с карты: переведено себе против внесённого", f"""
select {month_label("month")} as "Месяц", transferred as "Переведено на покупки", recorded as "Внесено «мимо счёта»",
       unreconciled as "Не разнесено", unreconciled_total as "Не разнесено всего"
from fin.v_card_reconcile order by month""", "table",
         colfmt(**{"Переведено на покупки": RUB, "Внесено «мимо счёта»": RUB, "Не разнесено": RUB, "Не разнесено всего": RUB}),
         "Перевод со счёта ИП себе на покупки — не расход; расход — сама покупка, внесённая формой «Добавить "
         "расход» как «мимо счёта». Перевод не на покупки — разметить формой «Разметить операцию».")
    card("c_ops", q, "Операции выписки и их разметка", """
select day as "Дата", amount_signed as "Сумма", article as "Статья банка", cash_class as "Вид денег",
       coalesce(transfer_kind, budget_item) as "Куда отнесли", left(operation_id, 8) as "Код"
from fin.v_bank_ops order by day desc, amount desc""", "table", colfmt(Сумма=RUB),
         "Код — первые 8 знаков операции: по нему форма «Разметить операцию» находит строку. Перевод себе без "
         "разметки — «на покупки».")
    card("c_uncl", q, "Не разобрано в выписке", """
select operation_date as "Дата", amount as "Сумма", direction as "Направление", issue as "Что не так"
from finance.v_unclassified order by operation_date desc""", "table", colfmt(Сумма=RUB),
         "Операции, не попавшие ни под одно правило cashflow. Пусто — всё разобрано.")
    card("c_holds", q, "В холде у банка", """
select operation_date as "Дата", amount_signed as "Сумма", days_in_hold as "Дней в холде"
from finance.v_pending order by operation_date desc""", "table", colfmt(Сумма=RUB),
         "Банк ещё не провёл: в остаток и ДДС не входят. Обновляется в 07:00 и 21:00 МСК.")
    card("c_claude", q, "Claude: счёт Anthropic против учёта бота", """
select day as "День (UTC)", round(bill_rub) as "Счёт Anthropic", round(bot_rub) as "Учёт бота",
       case when bill_rub is not null then round(unaccounted_rub) end as "Не учтено", case when bot_counted then 'да' else 'нет' end as "Бот вёл учёт"
from fin.v_claude_daily where day >= current_date - 29 order by day desc""", "table",
         colfmt(**{"Счёт Anthropic": RUB, "Учёт бота": RUB, "Не учтено": RUB}),
         "В рублях по вводной «Курс доллара». Счёт Anthropic приходит с задержкой в день-два: пока его нет, «не учтено» пусто. Не учтено — консоль, другие ключи организации или запросы, которые бот "
         "не записал. Доля за 7 дней (только дни, когда бот вёл учёт) > 20 % — строка в «Требует внимания».")
    card("c_yk", q, "Выплаты ЮKassa против реестра", """
select payout_date as "Выплата", bank_amount as "По выписке", registry_amount as "По реестру", diff as "Разница",
       case status when 'matched' then '🟢 сошлось' else '🔴 не сходится' end as "Сверка", days_from as "Платежи с",
       days_to as "по" from fin.v_yk_payouts order by payout_date desc""", "table",
         colfmt(**{"По выписке": RUB, "По реестру": RUB, "Разница": RUB}),
         "Выплата ЮKassa из выписки против суммы платежей реестра за дни, которые в неё вошли.")


def cards_reports(q):
    card("r_pnl", q, "Прибыль и убыток по месяцам пилота", """
select item as "Статья", m0 as "До пилота", m1 as "М1", m2 as "М2", m3 as "М3"
from fin.v_pnl_report order by sort""", "table",
         merge(colfmt(**{k: RUB for k in ("До пилота", "М1", "М2", "М3")}),
               {"table.column_formatting": [{"columns": ["Статья", "До пилота", "М1", "М2", "М3"], "type": "single",
                                             "operator": "=", "value": "ОПЕРАЦИОННАЯ ПРИБЫЛЬ", "color": "#509EE3",
                                             "highlight_row": True}]}),
         "Расходы со знаком минус. До пилота — с регистрации ИП до старта. Резерв УСН — 6 % дохода с начала года "
         "минус уплаченные взносы; взносы в прибыль не входят. Месяц, который не начался, — пусто.")
    card("r_ddc", q, "Движение денег: статья × месяц", f"""
select cash_class || ' · ' || item as "Статья", {month_label("month")} as "Месяц", amount as "Сумма"
from fin.v_ddc order by month, cash_class, item""", "table",
         {"table.pivot": True, "table.pivot_column": "Месяц", "table.cell_column": "Сумма", **colfmt(Сумма=RUB)},
         "По выписке банка, целые рубли. Операционные, Вложения (оборудование), Финансирование (взносы и выплаты "
         "владельцам), Переводы между своими счетами (в том числе себе на покупки).")
    card("r_flow", q, "Из чего сложился поток денег", """
with t as (select sum(operating) as op, sum(investing) as inv, sum(financing - loans) as fin_own, sum(loans) as loans,
                  sum(transfers) as tr from fin.v_cash_monthly)
select "Статья", round("Сумма") as "Сумма" from (
  select 1 as o, 'Операционные' as "Статья", op as "Сумма" from t
  union all select 2, 'Вложения', inv from t
  union all select 3, 'Взносы и выплаты владельцам', fin_own from t
  union all select 4, 'Кредит', loans from t where loans <> 0
  union all select 5, 'Переводы себе', tr from t
) x order by o""", "waterfall",
         {"graph.dimensions": ["Статья"], "graph.metrics": ["Сумма"], "waterfall.show_total": True,
          "waterfall.increase_color": GREEN, "waterfall.decrease_color": RED, "waterfall.total_color": "#509EE3",
          "graph.show_values": True, "graph.x_axis.axis_enabled": True, "graph.x_axis.title_text": "",
          "graph.y_axis.title_text": "₽", **colfmt(Сумма=RUB)},
         "С регистрации ИП. Итог — сколько денег на счёте прибавилось или убыло. Кредит — только если он был.")
    card("r_free_flow", q, "Сколько потратили за месяц", f"""
select {month_label("month")} as "Месяц", round(free_flow) as "Поток по выписке",
       round(free_flow_avg_3m) as "Среднее за 3 месяца"
from fin.v_cash_monthly order by month""", "combo",
         {"graph.dimensions": ["Месяц"], "graph.metrics": ["Поток по выписке", "Среднее за 3 месяца"],
          "series_settings": {"Поток по выписке": {"display": "bar", "color": "#509EE3"},
                              "Среднее за 3 месяца": {"display": "line", "color": GREY}},
          "graph.show_values": True, "graph.x_axis.title_text": "", "graph.y_axis.title_text": "₽",
          **colfmt(**{"Поток по выписке": RUB, "Среднее за 3 месяца": RUB})},
         "Операционные расходы и вложения по выписке за месяц (без взносов владельцев и переводов себе). Среднее — "
         "по доступным месяцам, не больше трёх. Покупки с карты сюда не входят — они в «Куда ушли деньги».")
    card("r_cac", q, "CAC по месяцам и сценарии", """
select period_start as "Месяц", spend_own as "Реклама", new_clients as "Новые клиенты", round(cac) as "CAC",
       round(first_order_after_cac) as "1-й заказ после CAC", round(after_cac_mid) as "При CAC 500"
from fin.v_cac where period_type = 'месяц' and new_clients is not null order by period_start""", "table",
         colfmt(**{"Реклама": RUB, "CAC": RUB, "1-й заказ после CAC": RUB, "При CAC 500": RUB}),
         "CAC = реклама своими + подрядчик / новые клиенты. «1-й заказ после CAC» — маржа первого заказа минус CAC; "
         "«При CAC 500» — та же маржа при сценарном CAC.")
    card("r_gates", q, "Ворота пилота", """
select gate as "Ворота", check_name as "Проверка",
       case when unit = '%' then fin.fmt_pct(value) else fin.fmt_rub(value) end as "Сейчас",
       case when unit = '%' then fin.fmt_pct(threshold) else fin.fmt_rub(threshold) end as "Порог",
       case status when 'зелёный' then '🟢' when 'красный' then '🔴' else '⚪ нет данных' end as "Статус",
       period_end as "Срок" from fin.v_pilot_gates order by sort""", "table", None,
         "Неделя 2, неделя 6 и М3 пилота. Пороги — вводные «Ворота …».")
    card("r_stock", q, "Склад", """
select coalesce(n.display_name, s.product) as "Товар", s.on_hand as "На руках", round(s.days_left) as "Хватит, дней",
       case when s.order_now then '🔴 пора заказывать' when s.reorder then '🟡 мало' end as "Сигнал",
       s.grams_left as "Чая осталось, г", s.packed as "Расфасовано"
from fin.v_stock s left join fin.product_names n using (product)
order by s.order_now desc nulls last, s.reorder desc, s.kind, 1""", "table", None,
         "Точка заказа = (срок поставки + фасовка и маркировка + страховой запас) × продажи в день за 14 дней.")
    card("r_codes", q, "Коды «Честного знака»", """
select gtin as "GTIN", imported as "Из СУЗ", available as "Свободно", used_in_orders as "В заказах",
       in_circulation as "В обороте", written_off as "Списано" from fin.v_codes order by gtin""", "table", None,
         "Коды по GTIN: импортировано из СУЗ, свободно, в заказах, введено в оборот, списано.")


THEMES = """case
    when key like 'capital%' or key in ('min_cash_reserve', 'cash_horizon_months', 'partner_payout_target')
         or key like 'bank%' then '1. Деньги'
    when key like 'acquiring%' or key in ('vat_on_fee', 'fiscal_rate', 'fiscal_units', 'usn_rate', 'planning_payment_method',
         'delivery_acquiring_markup') or key like 'ip_%' then '2. Цены и комиссии'
    when key like 'pouch%' or key like '%label%' or key in ('code_price', 'packing_loss_rate', 'packaging_limit',
         'codes_per_set4x35_packs', 'kin_codes_per_box', 'other_variable_per_order') or key like 'packaging%'
         then '3. Упаковка и коды'
    when key like '%deliver%' or key like 'inbound%' or key like 'supply%' or key like 'carrier%' or key in
         ('test_shipments', 'packing_marking_days', 'safety_stock_days') then '4. Логистика'
    when key like 'ad_%' or key like 'cac_%' or key like 'vk_%' or key like 'kpi_%' or key like 'api_%'
         or key in ('first_ad_test_own', 'usd_rub', 'scenario', 'contractor_paid_monthly', 'contractor_months')
         then '5. Реклама и привлечение'
    else '6. Пилот' end"""


def cards_inputs(q):
    card("i_inputs", q, "Вводные сейчас", f"""
select {THEMES} as "Тема", coalesce(label, key) as "Вводная",
       case when value is null then coalesce(value_text, 'нет данных')
            when unit = '%' then fin.fmt_pct(value)
            when unit like '₽%' then fin.fmt_rub(value) || coalesce(nullif(substring(unit from 2), ''), '')
            else trim_scale(value)::text || coalesce(' ' || nullif(unit, ''), '') end as "Значение",
       valid_from as "С какой даты",
       case when status ~* 'допущ|предлож|сценар|рабоч|нет данных|оценк|диапазон|временн|пример|проект|сверить|уточн'
            then 'допущение' else 'факт' end as "Факт или допущение",
       concat_ws(' · ', status, note) as "Комментарий", key as "Ключ"
from fin.inputs_current order by 1, 2""", "table",
         {"table.columns": [{"name": c, "enabled": c != "Ключ"} for c in
                            ("Тема", "Вводная", "Значение", "С какой даты", "Факт или допущение", "Комментарий", "Ключ")]},
         "Действующие сегодня вводные по темам. Изменить — форма «Изменить вводную» на вкладке «Ввод данных»: "
         "прежнее значение остаётся в истории ниже.")
    card("i_history", q, "История вводных", """
select coalesce(label, key) as "Вводная", coalesce(trim_scale(value)::text, value_text, 'нет данных') as "Значение",
       valid_from as "С какой даты", entered_by as "Кто", created_at as "Записано", note as "Комментарий"
from fin.inputs where key in (select key from fin.inputs group by key having count(*) > 1)
order by coalesce(label, key), valid_from desc, created_at desc""", "table", None,
         "Вводные, которые меняли: все значения с датой, с которой они действуют.")


# --- формы ------------------------------------------------------------------------------


def build_forms(qcoll, fin_db, tables):
    rows = call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {"query":
                "select 's', sku_name from fin.sku where active union all select 'b', bundle from fin.bundles where active"}})
    fb.SKUS = [r[1] for r in rows["data"]["rows"] if r[0] == "s"]
    fb.BUNDLES = [r[1] for r in rows["data"]["rows"] if r[0] == "b"]
    fb.KEYS = fb.input_keys(fin_db)
    models = fb.build_forms(qcoll, fin_db, tables)
    fb.make_actions(models, fin_db)
    # Разметка операции выписки: модель и форма.
    body = {"name": "Ввод: Разметка операций выписки", "display": "table", "visualization_settings": {},
            "collection_id": qcoll, "type": "model",
            "dataset_query": {"type": "query", "database": fin_db, "query": {"source-table": tables["bank_marks"]}}}
    models["bank_marks"] = call("POST", "/card", body)["id"]
    ops = call("POST", "/dataset", {"database": RO_DB, "type": "native", "native": {"query": """
select to_char(day, 'DD.MM.YYYY') || ' · ' || fin.fmt_rub(amount) || ' · ' || article || ' · ' || left(operation_id, 8)
from fin.v_bank_ops where direction = 'out' order by day desc limit 200"""}})["data"]["rows"]
    items = [r[0] for r in call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {
        "query": "select item from fin.budget_articles order by sort"}})["data"]["rows"]]
    fb.action("mark", models["bank_marks"], fin_db, "Разметить операцию", f"""
insert into fin.bank_marks (operation_id, transfer_kind, budget_item, note, entered_by)
select b.operation_id, {fb.OPT % 'kind'}, {fb.OPT % 'item'}, {fb.OPT % 'note'}, {{{{entered_by}}}}
from (select distinct operation_id from fin.v_bank_ops where left(operation_id, 8) = right({{{{op}}}}, 8)) b
""", [fb.field("op", "Операция (дата · сумма · статья · код)", "select", options=[r[0] for r in ops]),
      fb.field("kind", "Если это перевод себе — что это", "select", required=False,
               options=["на покупки", "вывод владельцем", "между своими счетами"]),
      fb.field("item", "Статья бюджета пилота", "select", required=False, options=items),
      fb.NOTE, fb.AUTHOR], "Операция размечена — расходы, ДДС и сверка покупок с карты пересчитаны.")
    return models


# --- раскладка --------------------------------------------------------------------------

_seq = [0]


def dc(key, tab, row, col, w, h, hide_empty=False):
    _seq[0] -= 1
    viz = {"card.hide_empty": True} if hide_empty else {}
    return {"id": _seq[0], "card_id": CARDS[key], "dashboard_tab_id": tab, "row": row, "col": col,
            "size_x": w, "size_y": h, "parameter_mappings": [], "visualization_settings": viz}


def text(tab, row, col, w, h, md, display="text"):
    _seq[0] -= 1
    return {"id": _seq[0], "card_id": None, "dashboard_tab_id": tab, "row": row, "col": col, "size_x": w, "size_y": h,
            "parameter_mappings": [],
            "visualization_settings": {"text": md, "virtual_card": {"name": None, "display": display, "archived": False,
                                                                     "visualization_settings": {}, "dataset_query": {}}}}


def button(key, label, tab, row, col, w=6, h=2):
    _seq[0] -= 1
    return {"id": _seq[0], "card_id": None, "action_id": fb.ACTIONS[key], "dashboard_tab_id": tab,
            "row": row, "col": col, "size_x": w, "size_y": h, "parameter_mappings": [],
            "visualization_settings": {"button.label": label, "actionDisplayType": "button",
                                       "virtual_card": {"display": "action", "visualization_settings": {},
                                                        "dataset_query": {}, "archived": False, "name": None}}}


def put(did, tabs, dashcards):
    call("PUT", f"/dashboard/{did}", {"tabs": [{"id": tid, "name": name} for name, tid in tabs.items()],
                                      "dashcards": dashcards})


def verify():
    bad = 0
    for key, cid in CARDS.items():
        result = call("POST", f"/card/{cid}/query", {})
        if result.get("status") != "completed" or result.get("error"):
            bad += 1
            print(f"  ✗ {key}: {str(result.get('error'))[:300]}")
    print("Вопросы: все выполняются" if not bad else f"Вопросов с ошибкой: {bad}")
    return bad


SERIES_COLORS = {}


if __name__ == "__main__":
    set_formatting()
    fin_db, tables = fb.fin_database()
    for _ in range(20):
        if "bank_marks" in tables:
            break
        fb.time.sleep(3)
        tables = {t["name"]: t["id"] for t in call("GET", f"/database/{fin_db}/metadata")["tables"] if t.get("schema") == "fin"}
    if "bank_marks" not in tables:
        raise SystemExit("Metabase не видит fin.bank_marks: миграция 009 применена (analytics/fin/apply.sh)?")
    root, qcoll, old = collections()
    pulse, ptabs = ensure_dashboard(root, PULSE, "Каждый день: всё ли в порядке, куда уходят деньги, работают ли реклама "
                                    "и бот, на чём зарабатываем. Выписка — 07:00 и 21:00 МСК, заказы — сразу.", PULSE_TABS)
    service, stabs = ensure_dashboard(root, SERVICE, "Раз в неделю и в конце месяца: ввод данных, сверки, отчёты, вводные.",
                                      SERVICE_TABS)
    links = {"money": f"{SITE}/dashboard/{pulse}?tab={ptabs['Деньги']}", "sales": f"{SITE}/dashboard/{pulse}?tab={ptabs['Продажи']}",
             "entry": f"{SITE}/dashboard/{service}?tab={stabs['Ввод данных']}",
             "checks": f"{SITE}/dashboard/{service}?tab={stabs['Сверки']}"}
    for item, color in call("POST", "/dataset", {"database": RO_DB, "type": "native", "native": {
            "query": "select item, color from fin.budget_articles"}})["data"]["rows"]:
        SERIES_COLORS[item] = {"color": color}

    build_forms(qcoll, fin_db, tables)
    cards_today(qcoll, links)
    cards_money(qcoll)
    cards_sales(qcoll)
    cards_product(qcoll)
    cards_entry(qcoll)
    cards_checks(qcoll)
    cards_reports(qcoll)
    cards_inputs(qcoll)

    T, M, S, P = ptabs["Сегодня"], ptabs["Деньги"], ptabs["Продажи"], ptabs["Товар"]
    put(pulse, ptabs, [
        # Сегодня: светофор, что сделать, остаток и прогноз.
        dc("t_free", T, 0, 0, 6, 3), dc("t_runway", T, 0, 6, 6, 3), dc("t_capital", T, 0, 12, 6, 3),
        dc("t_pilot", T, 0, 18, 6, 3),
        dc("t_attention", T, 3, 0, 24, 5),
        dc("t_balance", T, 8, 0, 24, 7),
        dc("t_soon", T, 15, 0, 24, 2, hide_empty=True),
        # Деньги
        dc("m_loss", M, 0, 0, 24, 2, hide_empty=True),
        dc("m_spend", M, 2, 0, 14, 8), dc("m_partners", M, 2, 14, 10, 4), dc("m_forecast", M, 6, 14, 10, 6),
        dc("m_budget", M, 10, 0, 14, 9),
        # Продажи — пока нет оплат, карточек нет.
        dc("s_funnel", S, 0, 0, 12, 7, True), dc("s_daily", S, 0, 12, 12, 7, True),
        dc("s_cac", S, 7, 0, 12, 6, True), dc("s_check", S, 7, 12, 12, 6, True),
        # Товар
        dc("p_waterfall", P, 0, 0, 12, 8), dc("p_margin", P, 0, 12, 12, 12),
        dc("p_breakeven_m1", P, 8, 0, 12, 2), dc("p_breakeven", P, 10, 0, 12, 4), dc("p_scenarios", P, 14, 0, 12, 4),
        dc("p_sales", P, 18, 0, 24, 6, True),
    ])
    E, C, R, I = stabs["Ввод данных"], stabs["Сверки"], stabs["Отчёты"], stabs["Вводные"]
    put(service, stabs, [
        text(E, 0, 0, 24, 1, "Каждый день", "heading"),
        button("ads", "Реклама за день", E, 1, 0), button("expense", "Добавить расход", E, 1, 6),
        text(E, 3, 0, 24, 1, "Редко", "heading"),
        button("capital", "Вклад / выплата партнёра", E, 4, 0), button("snapshot", "Остаток по выписке", E, 4, 6),
        button("lot", "Партия закупки", E, 4, 12), button("packing", "Фасовка", E, 4, 18),
        button("input", "Изменить вводную", E, 6, 0), button("mark", "Разметить операцию", E, 6, 6),
        button("act", "Акт перевозчика", E, 6, 12), button("lead", "Срок поставки", E, 6, 18),
        button("writeoff", "Списать код ЧЗ", E, 8, 0),
        dc("e_recent", E, 10, 0, 24, 8),
        dc("c_snapshot", C, 0, 0, 12, 3), dc("c_card", C, 0, 12, 12, 4),
        dc("c_ops", C, 4, 0, 24, 7), dc("c_claude", C, 11, 0, 12, 7), dc("c_yk", C, 11, 12, 12, 7, True),
        dc("c_uncl", C, 18, 0, 12, 5, True), dc("c_holds", C, 18, 12, 12, 5, True),
        dc("r_pnl", R, 0, 0, 24, 7), dc("r_ddc", R, 7, 0, 24, 7),
        dc("r_flow", R, 14, 0, 12, 7), dc("r_free_flow", R, 14, 12, 12, 7),
        dc("r_cac", R, 21, 0, 24, 4, True), dc("r_gates", R, 25, 0, 24, 5),
        dc("r_stock", R, 30, 0, 14, 8), dc("r_codes", R, 30, 14, 10, 8, True),
        dc("i_inputs", I, 0, 0, 24, 14), dc("i_history", I, 14, 0, 24, 6),
    ])

    failed = verify()
    # «Пульс» — главная страница, оба дашборда — первыми в «Дашбордах».
    call("PUT", f"/dashboard/{pulse}", {"collection_position": 1})
    call("PUT", f"/dashboard/{service}", {"collection_position": 2})
    call("PUT", "/setting/custom-homepage", {"value": True})
    call("PUT", "/setting/custom-homepage-dashboard", {"value": pulse})
    for c in old:
        call("PUT", f"/collection/{c['id']}", {"archived": True})
    print(f"{PULSE}: {SITE}/dashboard/{pulse}\n{SERVICE}: {SITE}/dashboard/{service}")
    raise SystemExit(1 if failed else 0)
