"""Дашборд «Пульс» в коллекции «Дашборды»: каждый день — «Сегодня», «Деньги», «Продажи», «Товар»;
раз в неделю и в конце месяца — «Ввод данных», «Сверки», «Отчёты», «Вводные», «Каталог».

    MB_FIN_PW="$(cat /etc/teashop/metabase_fin_password)" python3 dash_build.py

Один экран — один вопрос, ответ — в верхнем ряду. Все числа — из представлений fin
(миграции 009–010) через подключение metabase_ro; формы ввода — через «Финмодель (ввод)»
(роль metabase_fin), как в fin_build.py, функции которого здесь и используются.

Что делает:
- глобальные форматы Metabase: время 14:37, «32 731 ₽», «12,5 %»; даты в таблицах —
  текстом «08.09.2026» (у Metabase нет формата дня с ведущим нулём), на осях — «8 сен»;
- вопросы, модели и формы — заново в подколлекции «Пульс и Служебное — вопросы»
  (прежняя уходит в архив в конце, когда дашборды уже на новых вопросах);
- «Пульс» обновляется на месте: ссылка не меняется. До 24.10.2026 служебные вкладки
  были отдельным дашбордом «Служебное» — владелец попросил один экран, теперь они идут
  после «Товара», а «Служебное» уходит в архив;
- «Пульс» — главная страница Metabase и закреплён в «Дашбордах»;
- «Сводка», «Финансы» и «Служебное» со своими вопросами — в архив (ничего не удаляется).

Цвет — только у статуса, и только условным форматированием Metabase: эмодзи в данных
нет. Пороги датчика «Свободно» и цели прогресс-баров берутся из базы при сборке —
после смены вводных сборку перезапустить. Ручные правки дашбордов пропадут — правьте здесь.
"""
import fin_build as fb
from mbapi import BASE, call

SITE = BASE[:-4]
RO_DB = fb.RO_DB
ROOT = "Дашборды"
QUESTIONS = "Пульс и Служебное — вопросы"
PULSE = "Пульс"
# Служебные вкладки — после «Товара», в том же дашборде.
PULSE_TABS = ["Сегодня", "Деньги", "Продажи", "Товар", "Склад", "Ввод данных", "Сверки", "Отчёты", "Вводные", "Каталог"]
OLD_DASHBOARDS = ("Сводка", "Финансы", "Служебное")

# Рубли целые, проценты с одним знаком; разделители — из глобальных настроек
# (пробел для тысяч, запятая для дробей). Минус — знаком, а не скобками.
RUB = {"number_style": "decimal", "suffix": " ₽", "decimals": 0}
FULL = {"graph.label_value_formatting": "full"}
# Цвета статуса и нейтральные.
GREEN, YELLOW, RED, GREY, BLUE = "#84BB4C", "#F9CF48", "#ED6E6E", "#A7ADB4", "#509EE3"
# Месяц подписью «окт 2026», день — «8 сен».
RU_MONTHS = "(array['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'])"
AXIS_DAY = {"date_style": "D MMMM, YYYY", "date_abbreviate": True}


# Короткие имена статей для легенды графика: полные — в бюджете пилота.
SHORT_ITEMS = {"Первая закупка чая": "Чай", "Упаковка и расходники (лимит)": "Упаковка", "Термопринтер": "Принтер",
               "ВК: пополнение кабинета": "ВК", "Подрядчик за пилот": "Подрядчик", "Доставка основной партии": "Доставка партии",
               "ККТ, ФН, фискализация": "ККТ", "УКЭП / ЭДО / маркировка": "ЭДО и маркировка", "GTIN / карточки товаров": "GTIN",
               "Сервисы за пилот": "Сервисы", "Взносы 2026: расчётный резерв": "Взносы", "Обслуживание банка за пилот": "Банк",
               "API тестов и консультаций до продаж": "API до продаж", "Тестовые отправки до рекламы": "Тест-отправки",
               "Не сопоставлено": "Без статьи"}


def short_item(expr):
    whens = " ".join(f"when '{k}' then '{v}'" for k, v in SHORT_ITEMS.items())
    return f"case {expr} {whens} else {expr} end"


def month_label(expr):
    return f"{RU_MONTHS}[extract(month from {expr})::int] || ' ' || extract(year from {expr})::int"


def d(expr):
    """Дата в таблице: «08.09.2026» (глобальный формат Metabase даёт «8.9.2026»)."""
    return f"to_char({expr}, 'DD.MM.YYYY')"


def rub(x):
    """«30 000 ₽» для заголовков и подписей, которые собираются при сборке."""
    return f"{round(float(x or 0)):,} ₽".replace(",", " ")


def colfmt(**cols):
    return {"column_settings": {f'["name","{name}"]': fmt for name, fmt in cols.items()}}


def merge(*dicts):
    out = {}
    for x in dicts:
        for k, v in x.items():
            out[k] = {**out[k], **v} if k == "column_settings" and k in out else v
    return out


def color_rules(column, mapping, *, row=False):
    """Условный цвет ячейки (или строки) по тексту: {"Срочно": RED, …}."""
    return {"table.column_formatting": [
        {"columns": [column], "type": "single", "operator": "=", "value": value, "color": color, "highlight_row": row}
        for value, color in mapping.items()]}


# --- глобальные настройки -------------------------------------------------------------


def set_formatting():
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
                                         "description": "Вопросы, модели и формы «Пульса». Собирает dash_build.py."})
    return root["id"], qcoll["id"], old


def ensure_dashboard(root, name, desc, tabs):
    items = call("GET", f"/collection/{root}/items?models=dashboard")["data"]
    existing = fb.find(items, name=name)
    did = existing["id"] if existing else call("POST", "/dashboard", {"name": name, "collection_id": root})["id"]
    call("PUT", f"/dashboard/{did}", {"description": desc, "width": "full"})
    current = {t["name"]: t["id"] for t in call("GET", f"/dashboard/{did}").get("tabs", [])}
    body = [{"id": current.get(t, -(i + 1)), "name": t} for i, t in enumerate(tabs)]
    # Вкладки сначала без карточек: id вкладок нужны ссылкам «Требует внимания».
    call("PUT", f"/dashboard/{did}", {"tabs": body, "dashcards": []})
    return did, {t["name"]: t["id"] for t in call("GET", f"/dashboard/{did}")["tabs"]}


def archive_old(root):
    """«Сводка» и «Финансы» — в архив вместе с вопросами; ничего не удаляется."""
    for dash in call("GET", "/dashboard/"):
        if dash["name"] in OLD_DASHBOARDS:
            call("PUT", f"/dashboard/{dash['id']}", {"archived": True})
            print(f"  в архив: {dash['name']} (/dashboard/{dash['id']})")
    # Вопросы «Сводки» лежат прямо в «Дашбордах»; вопросы «Пульса» — в своей подколлекции.
    cards = [i for i in call("GET", f"/collection/{root}/items?models=card&models=dataset")["data"]]
    for item in cards:
        call("PUT", f"/card/{item['id']}", {"archived": True})
    if cards:
        print(f"  в архив: вопросы «Сводки» — {len(cards)}")
    for coll in call("GET", "/collection"):
        if coll.get("name") == "Финансы" and coll.get("location") == "/" and not coll.get("archived"):
            call("PUT", f"/collection/{coll['id']}", {"archived": True})
            print(f"  в архив: коллекция «Финансы» ({coll['id']})")


# --- вопросы --------------------------------------------------------------------------

CARDS = {}


def card(key, coll, name, sql, display, viz=None, description=None, tags=None):
    body = {"name": name, "display": display, "collection_id": coll, "description": description,
            "visualization_settings": merge(FULL, viz or {}),
            "dataset_query": {"type": "native", "database": RO_DB,
                              "native": {"query": sql.strip(), "template-tags": tags or {}}}}
    CARDS[key] = call("POST", "/card", body)["id"]
    return CARDS[key]


def number(key, coll, name, sql, description, fmt=None):
    """Плитка-число одним шрифтом: без компактных «33k»."""
    viz = {"scalar.compact_primary_number": False}
    if fmt:
        viz.update(colfmt(**fmt))
    return card(key, coll, name, sql, "scalar", viz, description)


def one_row(sql):
    rows = call("POST", "/dataset", {"database": RO_DB, "type": "native", "native": {"query": sql}})["data"]["rows"]
    return rows[0] if rows else None


def cards_today(q, links):
    m = one_row("select free_cash, reserve, left_before_start, capital_remaining from fin.v_money_now")
    free, reserve, left, _capital = (float(x or 0) for x in m)
    top = max(left * 1.5, free * 1.2, reserve)
    low = min(0.0, free * 1.2)
    card("t_free", q, f"Свободно сверх резерва {rub(reserve)}", """
select free_cash as "Свободно" from fin.v_money_now""", "gauge",
         {"gauge.segments": ([{"min": low, "max": 0, "color": RED, "label": "ниже резерва"}] if low < 0 else []) + [
             {"min": 0, "max": left, "color": RED, "label": "меньше трат до старта"},
             {"min": left, "max": top, "color": GREEN, "label": "хватает на траты до старта"}],
          **colfmt(Свободно=RUB)},
         "Остаток по выписке банка минус минимальный резерв (вводная). Красное — свободных денег меньше, чем "
         "осталось потратить до старта по бюджету пилота; зелёное — хватает. Границы шкалы — на момент сборки "
         "дашборда. Выписка обновляется в 07:00 и 21:00 МСК.")
    number("t_runway", q, "Денег хватит до", """
select runway_text from fin.v_money_now""",
           "Первый день, когда прогноз остатка опускается ниже резерва; если не опускается — «на весь прогноз» "
           "с датой его конца (конец пилота + 30 дней). Плановые платежи формы «Добавить расход» — на свои даты. Прогноз учитывает ожидаемые вклады (вводная «Когда ждём оставшиеся "
           "вклады»), закупку чая и пополнение ВК на их даты, остальной бюджет пилота, сервисы, банк, налоги. "
           "Пока вклады не пришли, ниже — тот же прогноз без них.")
    plan = one_row("select plan from fin.v_capital_status")
    card("t_capital", q, "Вклады партнёров", """
select contributed as "Внесено" from fin.v_capital_status""", "progress",
         {"progress.goal": float(plan[0] or 300000), "progress.color": BLUE, **colfmt(Внесено=RUB)},
         "Внесено: «Взнос собственника» по выписке — чей он, видно на вкладке «Деньги». Цель — вводная "
         "«Стартовый капитал».")
    start = one_row(f"select {d('start_date')} from fin.v_pilot_status")[0]
    number("t_pilot", q, f"Дней до старта {start[:5]}", """
select days_to_start from fin.v_pilot_status where not started""",
           "Сколько дней до старта пилота (вводная «Первый месяц календарного плана»). После старта на этом месте — "
           "убыток пилота против паузы и стопа.")
    loss = one_row("select pilot_pause_loss, pilot_max_loss from fin.v_pilot_status")
    card("t_loss", q, f"Убыток пилота: пауза {rub(loss[0])}, стоп {rub(loss[1])}", """
select pilot_loss as "Убыток" from fin.v_pilot_status where started and pilot_loss is not null""", "progress",
         {"progress.goal": float(loss[1] or 30000), "progress.color": RED, **colfmt(Убыток=RUB)},
         "Накопленный убыток М1–М3 по P&L против вводных «Убыток для паузы» и «Максимальный убыток». До старта "
         "карточки нет.")
    number("t_nocap", q, "Без ожидаемых вкладов денег хватит до", f"""
select case when runout_no_capital <= bank_date then 'уже ниже резерва'
            else coalesce({d('runout_no_capital')}, 'на весь прогноз (до ' || {d('horizon')} || ')') end
from fin.v_money_now where capital_remaining > 0""",
           "Тот же прогноз, но без вкладов, которые ещё не пришли. Карточка пропадает, когда весь стартовый капитал внесён.")
    card("t_attention", q, "Требует внимания", f"""
select level as "Уровень", text as "Что", case place when 'money' then 'Деньги' when 'entry' then 'Ввод данных'
            when 'checks' then 'Сверки' when 'sales' then 'Продажи' when 'stock' then 'Склад' end as "Где",
       case place when 'money' then '{links["money"]}' when 'entry' then '{links["entry"]}'
            when 'checks' then '{links["checks"]}' when 'sales' then '{links["sales"]}'
            when 'stock' then '{links["stock"]}' end as "Ссылка"
from fin.v_attention order by case level when 'Срочно' then 0 when 'Важно' then 1 else 2 end, sort""", "table",
         merge({"table.columns": [{"name": c, "enabled": c != "Ссылка"} for c in ("Уровень", "Что", "Где", "Ссылка")]},
               colfmt(Где={"view_as": "link", "link_text": "{{Где}}", "link_url": "{{Ссылка}}"}),
               color_rules("Уровень", {"Срочно": RED, "Важно": YELLOW, "Всё в порядке": GREEN})),
         "Проверки: деньги на траты до старта, покупки с карты без записи «мимо счёта», переводы себе без разметки, "
         "операции без статьи бюджета, вклад без партнёра, партия чая, «Не разобрано», сверка остатка, Claude, "
         "чеки и выплаты ЮKassa; после старта — заказы не у перевозчика и вопросы менеджеру. «Где» — ссылка на вкладку.")
    card("t_balance", q, "Остаток и прогноз", """
select day as "День", actual as "Остаток", forecast as "Прогноз" from fin.v_balance_chart order by day""", "line",
         {"graph.dimensions": ["День"], "graph.metrics": ["Остаток", "Прогноз"],
          "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Деньги, ₽", "line.marker_enabled": False,
          "graph.show_goal": True, "graph.goal_value": reserve,
          "graph.goal_label": f"резерв {rub(reserve)}",
          "series_settings": {"Остаток": {"color": BLUE}, "Прогноз": {"color": GREY, "line.style": "dashed"}},
          **colfmt(Остаток=RUB, Прогноз=RUB, День=AXIS_DAY)},
         "Сплошная — остаток по выписке на конец дня, пунктир — прогноз до конца пилота + 30 дней, как в «Денег "
         "хватит до». Ступенька вверх — ожидаемые вклады в день из вводной «Когда ждём оставшиеся вклады», вниз — "
         "закупка чая и пополнение ВК в дни из вводных. Линия — минимальный резерв.")
    number("t_soon", q, " ", """
select 'Продажи, воронка и CAC появятся здесь после ' || to_char(fin.inp_date('pilot_start_date'), 'DD.MM')
where not exists (select 1 from analytics.v_orders where is_paid)""",
           "Карточка пропадает с первым оплаченным заказом.")


def cards_money(q):
    card("m_spend", q, "Куда ушли деньги по месяцам", f"""
select {month_label("date_trunc('month', s.day)")} as "Месяц",
       {short_item("s.budget_item")} || case when b.cash_class = 'Вложения' then ' (влож.)' else '' end as "Статья",
       round(sum(s.amount)) as "Расход"
from fin.v_spend s join fin.budget_articles b on b.item = s.budget_item
group by date_trunc('month', s.day), s.budget_item, b.cash_class, b.sort order by date_trunc('month', s.day), b.sort""",
         "bar", {"graph.dimensions": ["Месяц", "Статья"], "graph.metrics": ["Расход"], "stackable.stack_type": "stacked",
                 "graph.show_values": True, "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Расход, ₽",
                 "series_settings": SERIES_COLORS, **colfmt(Расход=RUB)},
         "Расходы бизнеса: выписка (без переводов себе) + покупки «мимо счёта» по статьям бюджета пилота. Переводы "
         "себе на покупки сюда не входят — их сверка на вкладке «Сверки». Вложения — оборудование.")
    card("m_partners", q, "Кто сколько вложил", """
select partner as "Партнёр", plan as "План", contributed as "Внесено",
       greatest(coalesce(plan, 0) - contributed, 0) as "Осталось",
       round(100 * contributed / nullif(plan, 0)) || ' %' as "Внесено, %"
from fin.v_partner_capital
union all
select 'Всего', plan, contributed, remaining, round(100 * share) || ' %' from fin.v_capital_status""", "table",
         colfmt(**{"План": RUB, "Внесено": RUB, "Осталось": RUB}),
         "План — вводные «Вклад Никиты» и «Вклад Ильи». Чей вклад — разметкой операции «Взнос собственника» "
         "(форма «Разметить операцию», поле «Чей вклад»); не размеченный — строкой «Не указано, чей».")
    card("m_budget", q, "Бюджет пилота: сколько осталось", """
with b as (select * from fin.v_pilot_budget_status
           where not (coalesce(plan, 0) = 0 and coalesce(spent, 0) = 0 and coalesce(planned, 0) = 0))
select item as "Статья", plan as "План", spent_before_start as "До старта", spent_in_pilot as "В пилоте",
       planned as "Запланировано", left_amount as "Осталось" from (
    select item, plan, spent_before_start, spent_in_pilot, planned, left_amount, sort from b
    union all
    select 'Всего', sum(plan), sum(spent_before_start), sum(spent_in_pilot), sum(planned), sum(left_amount), 1000 from b
) x order by sort""", "table",
         merge(colfmt(**{"План": RUB, "До старта": RUB, "В пилоте": RUB, "Запланировано": RUB, "Осталось": RUB}),
               {"table.column_formatting": [
                   {"columns": ["Осталось"], "type": "single", "operator": "<", "value": 0, "color": RED, "highlight_row": False}]}),
         "План — статьи бюджета пилота из вводных; потрачено — выписка и «мимо счёта», сведённые к статьям, "
         "отдельно до старта и в пилоте; запланировано — плановые расходы, ещё не закрытые фактом. "
         "Осталось = план − потрачено − запланировано. Статьи без плана и трат скрыты. "
         "«Не сопоставлено» — траты без статьи: разметить формой «Разметить операцию».")
    card("m_forecast", q, "Деньги на 8 недель вперёд", f"""
select {d("week_start")} as "Неделя с", round(opening) as "На начало", round(inflow) as "Придёт",
       round(outflow) as "Уйдёт", round(closing) as "На конец",
       case when below_reserve then 'ниже резерва' else 'ок' end as "Резерв"
from fin.v_weekly_forecast order by week_no""", "table",
         merge(colfmt(**{"На начало": RUB, "Придёт": RUB, "Уйдёт": RUB, "На конец": RUB}),
               color_rules("Резерв", {"ниже резерва": RED, "ок": GREEN})),
         "Тот же прогноз, что «Денег хватит до»: придёт — вклады и выплаты ЮKassa; уйдёт — закупка чая и пополнение "
         "ВК в свои дни, плановые платежи в свои дни, остальной бюджет пилота, сервисы, банк, подрядчик, налоги.")
    start = one_row("select to_char(fin.inp_date('pilot_start_date'), 'DD.MM') ")[0]
    number("m_ahead_total", q, f"До старта {start} нужно заплатить", """
select fin.fmt_rub(coalesce(sum(amount), 0)) from fin.v_payments_ahead
where not closed and day < fin.inp_date('pilot_start_date')""",
           "Открытые плановые платежи (форма «Добавить расход», «план») и налоги из календаря до даты старта.")
    card("m_ahead", q, "Платежи впереди: 8 недель", f"""
select {d("day")} as "Когда", what as "Что", amount as "Сумма", coalesce(budget_item, source) as "Статья",
       case when closed then 'закрыт' when overdue then 'просрочен' else 'ждёт' end as "Статус"
from fin.v_payments_ahead order by day, what""", "table",
         merge(colfmt(Сумма=RUB), color_rules("Статус", {"закрыт": GREEN, "просрочен": RED, "ждёт": GREY})),
         "Плановые расходы и налоги на 8 недель. Просроченный план стоит на завтра, пока его не закроет факт: "
         "поле «Закрывает план» в «Добавить расход» или «Разметить операцию».")
    grain = {"grain": {"id": "grain", "name": "grain", "display-name": "Период", "type": "text", "required": False,
                       "default": "месяц"}}
    flows = "fin.cash_flows([[{{grain}} ||]] '')"
    card("m_fcf", q, "Денежный поток и FCF", f"""
select to_char(period, 'DD.MM.YYYY') as "Период с",
       round(operating) as "Операционный", round(investing) as "Вложения", round(fcf) as "FCF",
       round(financing) as "Финансирование", round(unallocated) as "Не разнесено с карты",
       round(operating_per_order) as "Операционный на заказ"
from {flows} order by period""", "table",
         colfmt(**{c: RUB for c in ("Операционный", "Вложения", "FCF", "Финансирование", "Не разнесено с карты",
                                     "Операционный на заказ")}),
         "FCF = операционный поток + вложения (оборудование). Финансирование — вклады, выплаты партнёрам, переводы "
         "между своими счетами — в FCF не входит. Перевод себе на покупки разнесён по тому, что на него купили "
         "«мимо счёта»; не разнесённое — в операционных. На заказ — после старта. Месяцы или недели — фильтр «Период».",
         tags=grain)
    card("m_fcf_cum", q, "Накопленный FCF против вкладов партнёров", f"""
select period as "Период", round(cum_fcf) as "Накопленный FCF", round(cum_contributions) as "Вклады партнёров"
from {flows} order by period""", "line",
         # Одна ось: иначе Metabase уводит вклады на вторую шкалу, и линии не сравнить.
         {"graph.dimensions": ["Период"], "graph.metrics": ["Накопленный FCF", "Вклады партнёров"],
          "graph.y_axis.auto_split": False,
          "graph.x_axis.title_text": "", "graph.y_axis.title_text": "₽",
          **colfmt(**{"Накопленный FCF": RUB, "Вклады партнёров": RUB})},
         "Сколько бизнес потратил (FCF накопленный, ниже нуля) против того, сколько партнёры вложили.", tags=grain)


def cards_sales(q):
    paid = "exists (select 1 from analytics.v_orders where is_paid)"
    number("s_soon", q, " ", f"""
select 'Продаж ещё нет — старт ' || to_char(fin.inp_date('pilot_start_date'), 'DD.MM')
where not {paid}""",
           "Здесь будут: воронка от диалога до вручения, заказы по дням против плана, CAC по неделям с целью и "
           "лимитом (вводные «Цель: CAC», «Лимит: CAC»), средний чек с целью. Карточка пропадает с первым оплаченным "
           "заказом, вместо неё появляются графики.")
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
with dd as (select x::date as day from generate_series(fin.inp_date('pilot_start_date'), current_date, interval '1 day') x),
o as (select paid_date_msk as day, count(*) as n from analytics.v_orders where is_paid and not is_refunded group by 1)
select dd.day as "День", coalesce(o.n, 0) as "Заказов",
       (select b.orders_needed / 30.0 from fin.v_breakeven_plan b where b.period_start <= dd.day
        order by b.period_start desc limit 1) as "План"
from dd left join o using (day) where {paid} order by 1""", "combo",
         {"graph.dimensions": ["День"], "graph.metrics": ["Заказов", "План"],
          "series_settings": {"Заказов": {"display": "bar", "color": BLUE},
                              "План": {"display": "line", "color": GREEN, "line.style": "dashed"}},
          "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Заказов в день", "graph.show_values": True,
          **colfmt(День=AXIS_DAY)},
         "Оплаченные заказы без возвратов по дню оплаты. План — заказов для безубыточности месяца / 30.")
    cac = one_row("select goal, limit_value from fin.v_kpi_targets where metric = 'cac'")
    card("s_cac", q, "CAC по неделям", """
select c.period_start as "Неделя", round(c.cac_paid) as "CAC", t.limit_value as "Лимит"
from fin.v_cac c cross join fin.v_kpi_targets t
where t.metric = 'cac' and c.period_type = 'неделя' and c.period_start >= date_trunc('week', fin.inp_date('pilot_start_date'))
  and c.cac_paid is not null order by 1""", "line",
         {"graph.dimensions": ["Неделя"], "graph.metrics": ["CAC", "Лимит"],
          "series_settings": {"CAC": {"color": BLUE}, "Лимит": {"color": RED, "line.style": "dashed"}},
          "graph.show_goal": True, "graph.goal_value": float(cac[0] or 500), "graph.goal_label": "цель",
          "graph.show_values": True, "graph.x_axis.title_text": "", "graph.y_axis.title_text": "CAC, ₽",
          **colfmt(CAC=RUB, Лимит=RUB, Неделя=AXIS_DAY)},
         "Реклама своими деньгами за неделю / новые клиенты, пришедшие по рекламной ссылке. Бонусы ВК не входят. "
         "Цель и лимит — вводные «Цель: CAC» и «Лимит: CAC».")
    goal = one_row("select goal from fin.v_kpi_targets where metric = 'avg_check'")
    card("s_check", q, "Средний чек по неделям", """
select date_trunc('week', paid_date_msk)::date as "Неделя", round(avg(total)) as "Средний чек"
from analytics.v_orders where is_paid and not is_refunded group by 1 order by 1""", "line",
         {"graph.dimensions": ["Неделя"], "graph.metrics": ["Средний чек"],
          "series_settings": {"Средний чек": {"color": BLUE}},
          "graph.show_goal": True, "graph.goal_value": float(goal[0] or 1490), "graph.goal_label": "цель",
          "graph.show_values": True, "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Средний чек, ₽",
          **colfmt(**{"Средний чек": RUB, "Неделя": AXIS_DAY})},
         "Оплаченные заказы без возвратов, с доставкой. Цель — вводная «Цель: средний чек» (цена «Точки входа»).")


def cards_product(q):
    lead = (one_row("select value_text from fin.inputs_current where key = 'lead_product'") or ["Набор «Точка входа»"])[0]
    api = one_row("select round(fin.api_per_new_buyer()) from fin.inputs limit 1")
    card("p_waterfall", q, f"Из чего складывается маржа «{lead.replace('Набор ', '').strip('«»')}»", """
select step as "Шаг", round(amount) as "Сумма" from fin.v_unit_waterfall
where product = (select value_text from fin.inputs_current where key = 'lead_product') order by step_no""", "waterfall",
         {"graph.dimensions": ["Шаг"], "graph.metrics": ["Сумма"], "waterfall.show_total": True,
          "waterfall.increase_color": GREEN, "waterfall.decrease_color": RED, "waterfall.total_color": BLUE,
          "graph.show_values": True, "graph.x_axis.axis_enabled": True, "graph.x_axis.title_text": "",
          "graph.y_axis.title_text": "На один набор, ₽", **colfmt(Сумма=RUB)},
         f"Последний столбец — маржа до рекламы = цена × (1 − УСН − эквайринг с чеком и НДС) − чай − упаковка и "
         f"коды − доставка и прочее на заказ − Claude. Claude в себестоимости — {api[0] if api else 'нет данных'} ₽ "
         f"на нового покупателя: API на консультацию × курс / конверсия. Источник — fin.v_unit_cost.")
    card("p_margin", q, "Маржа до рекламы по товарам", """
select name || case kind_group when 'Блины' then ' · блин' else '' end
            || case when sold_by_bot then ' · бот' else '' end as "Товар",
       case when not low_margin then round(margin_before_cac) end as "Маржа",
       case when low_margin then round(margin_before_cac) end as "Ниже порога"
from fin.v_product_card order by kind_sort, margin_before_cac desc""", "row",
         {"graph.dimensions": ["Товар"], "graph.metrics": ["Маржа", "Ниже порога"], "stackable.stack_type": "stacked",
          "graph.max_categories_enabled": False, "graph.show_values": True,
          "series_settings": {"Маржа": {"color": GREEN}, "Ниже порога": {"color": RED}},
          "graph.x_axis.title_text": "", "graph.y_axis.title_text": "Маржа на единицу, ₽",
          **colfmt(Маржа=RUB, **{"Ниже порога": RUB})},
         "Сверху вниз: наборы, сеты, пачки (без пометки), блины. Красным — маржа до рекламы ниже порога (вводная «Порог низкой "
         "маржи», 20 % цены). «бот» — товар есть в каталоге бота. Имена сокращены: блины — «Пуэр Банжань Гушу» "
         "(>3 лет, 357 г, IT-75) и «Пуэр Танцующая Пара» (Баньна Фенг Мэнхай, 357 г, IT-101).")
    need = one_row("select string_agg(month_label || ' — ' || orders_needed, ', ' order by month_no) from fin.v_breakeven_plan")
    card("p_breakeven", q, "Безубыточность по месяцам пилота", """
select month_label as "Месяц", round(fixed_costs) as "Постоянные", ads_plan as "Реклама по плану",
       round(lead_margin) as "Маржа набора", orders_needed as "Нужно заказов в месяц"
from fin.v_breakeven_plan order by month_no""", "table",
         colfmt(**{"Постоянные": RUB, "Реклама по плану": RUB, "Маржа набора": RUB}),
         f"Сейчас нужно заказов «Точки входа» в месяц: {need[0] if need and need[0] else 'нет данных'}. "
         "Нужно заказов = ⌈(постоянные + реклама по плану) / маржа до рекламы⌉; постоянные — сервисы, банк, "
         "взносы / 12, подрядчик; реклама — вводные «Реклама по плану: М1…М3», свои деньги, без бонусов.")
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
select to_char(created_at at time zone 'Europe/Moscow', 'DD.MM.YYYY HH24:MI') as "Когда", entered_by as "Кто",
       form as "Форма", what as "Что", note as "Примечание"
from fin.v_recent_entries order by created_at desc""", "table", None, "Всё, что внесли формами, — сверху новое.")


def cards_checks(q):
    card("c_snapshot", q, "Остаток: снимок против банка", f"""
select {d("snapshot_date")} as "Снимок на", snapshot_balance as "По снимку", snapshot_balance - snapshot_diff as "По банку",
       snapshot_diff as "Расхождение" from fin.v_cash_status""", "table",
         colfmt(**{"По снимку": RUB, "По банку": RUB, "Расхождение": RUB}),
         "Последний снимок формы «Остаток по выписке» против остатка, который cashflow собрал из выписки.")
    card("c_card", q, "Покупки с карты: переведено себе против внесённого", f"""
select {month_label("month")} as "Месяц", transferred as "Переведено на покупки", recorded as "Внесено «мимо счёта»",
       unreconciled as "Не разнесено", unreconciled_total as "Не разнесено всего"
from fin.v_card_reconcile order by month""", "table",
         colfmt(**{"Переведено на покупки": RUB, "Внесено «мимо счёта»": RUB, "Не разнесено": RUB, "Не разнесено всего": RUB}),
         "Перевод со счёта ИП себе на покупки — не расход; расход — сама покупка, внесённая формой «Добавить расход» "
         "как «мимо счёта». Перевод не на покупки — разметить формой «Разметить операцию».")
    card("c_ops", q, "Операции выписки и их разметка", f"""
select {d("day")} as "Дата", amount_signed as "Сумма", article as "Статья банка", cash_class as "Вид денег",
       coalesce(transfer_kind || case when transfer_kind_default then ' (по умолчанию — проверьте)' else '' end,
                case when partner is not null then 'вклад: ' || partner end, budget_item) as "Куда отнесли",
       left(operation_id, 8) as "Код"
from fin.v_bank_ops order by day desc, amount desc""", "table",
         merge(colfmt(Сумма=RUB),
               {"table.column_formatting": [{"columns": ["Куда отнесли"], "type": "single", "operator": "contains",
                                             "value": "проверьте", "color": YELLOW, "highlight_row": False},
                                            {"columns": ["Куда отнесли"], "type": "single", "operator": "=",
                                             "value": "Не сопоставлено", "color": YELLOW, "highlight_row": False}]}),
         "Код — первые 8 знаков операции: по нему форма «Разметить операцию» находит строку. Перевод себе без "
         "разметки считается покупкой только по умолчанию — подтвердите или поменяйте вид формой.")
    card("c_uncl", q, "Не разобрано в выписке", f"""
select {d("operation_date")} as "Дата", amount as "Сумма", direction as "Направление", issue as "Что не так"
from finance.v_unclassified order by operation_date desc""", "table", colfmt(Сумма=RUB),
         "Операции, не попавшие ни под одно правило cashflow. Пусто — всё разобрано.")
    card("c_holds", q, "В холде у банка", f"""
select {d("operation_date")} as "Дата", amount_signed as "Сумма", days_in_hold as "Дней в холде"
from finance.v_pending order by operation_date desc""", "table", colfmt(Сумма=RUB),
         "Банк ещё не провёл: в остаток и ДДС не входят. Обновляется в 07:00 и 21:00 МСК.")
    card("c_claude", q, "Claude: счёт Anthropic против учёта бота", f"""
select {d("day")} as "День (UTC)", round(bill_rub) as "Счёт Anthropic", round(bot_rub) as "Учёт бота",
       case when comparable then round(unaccounted_rub) end as "Не учтено",
       case when comparable then 'да' when bill_rub is null then 'счёт ещё не пришёл'
            when not bot_counted then 'бот не вёл учёт' else 'неполный день' end as "Сравнимо"
from fin.v_claude_daily where day >= current_date - 29 order by day desc""", "table",
         colfmt(**{"Счёт Anthropic": RUB, "Учёт бота": RUB, "Не учтено": RUB}),
         "В рублях по вводной «Курс доллара». Сравнимы только полные дни учёта бота с уже загруженным счётом "
         "(счёт приходит с задержкой; первый день учёта и сегодня — неполные). Не учтено > 20 % хотя бы за 3 таких "
         "дня из последних 7 — строка в «Требует внимания».")
    card("c_yk", q, "Выплаты ЮKassa против реестра", f"""
select {d("payout_date")} as "Выплата", bank_amount as "По выписке", registry_amount as "По реестру", diff as "Разница",
       case status when 'matched' then 'сошлось' else 'не сходится' end as "Сверка",
       {d("days_from")} || '–' || {d("days_to")} as "Платежи за"
from fin.v_yk_payouts order by payout_date desc""", "table",
         merge(colfmt(**{"По выписке": RUB, "По реестру": RUB, "Разница": RUB}),
               color_rules("Сверка", {"сошлось": GREEN, "не сходится": RED})),
         "Выплата ЮKassa из выписки против суммы платежей реестра за дни, которые в неё вошли.")


def cards_reports(q):
    pnl_cols = ("До пилота", "М1", "М2", "М3")
    fmt_cell = ("case when {0} is null then '—' when sort = 1 then trim_scale({0})::text || ' шт.' "
                "else replace(fin.fmt_rub({0}), '-', '−') end")
    card("r_pnl", q, "Прибыль и убыток по месяцам пилота", f"""
select item as "Статья", {fmt_cell.format('m0')} as "До пилота", {fmt_cell.format('m1')} as "М1",
       {fmt_cell.format('m2')} as "М2", {fmt_cell.format('m3')} as "М3"
from fin.v_pnl_report order by sort""", "table",
         {"column_settings": {f'["name","{c}"]': {"text_align": "right"} for c in pnl_cols}},
         "Расходы со знаком минус. До пилота — с регистрации ИП до старта. Резерв УСН — 6 % дохода с начала года "
         "минус уплаченные взносы; взносы в прибыль не входят. «—» — месяц ещё не начался.")
    card("r_ddc", q, "Движение денег: статья × месяц", f"""
select cash_class || ' · ' || item as "Статья", {month_label("month")} as "Месяц", amount as "Сумма"
from fin.v_ddc order by month, cash_class, item""", "table",
         {"table.pivot": True, "table.pivot_column": "Месяц", "table.cell_column": "Сумма", **colfmt(Сумма=RUB)},
         "По выписке банка, целые рубли. Операционные, Вложения (оборудование), Финансирование (взносы и выплаты "
         "владельцам), Переводы между своими счетами (в том числе себе на покупки).")
    card("r_flow", q, "Из чего сложился остаток на счёте", """
with t as (select sum(operating) as op, sum(investing) as inv, sum(financing - loans) as fin_own, sum(loans) as loans,
                  sum(transfers) as tr from fin.v_cash_monthly)
select "Статья", round("Сумма") as "Сумма" from (
  select 1 as o, 'Взносы' as "Статья", fin_own as "Сумма" from t
  union all select 2, 'Кредит', loans from t where loans <> 0
  union all select 3, 'Расходы', op from t
  union all select 4, 'Вложения', inv from t
  union all select 5, 'Переводы себе', tr from t
) x order by o""", "waterfall",
         {"graph.dimensions": ["Статья"], "graph.metrics": ["Сумма"], "waterfall.show_total": True,
          "waterfall.increase_color": GREEN, "waterfall.decrease_color": RED, "waterfall.total_color": BLUE,
          "graph.show_values": True, "graph.x_axis.axis_enabled": True, "graph.x_axis.title_text": "",
          "graph.y_axis.title_text": "₽", **colfmt(Сумма=RUB)},
         "По выписке с регистрации ИП: взносы владельцев (минус выплаты), операционные расходы, вложения "
         "(оборудование), переводы себе. Итог — остаток на счёте. Кредит — только если он был.")
    card("r_spent", q, "Сколько потратили за месяц", f"""
with m as (select date_trunc('month', day)::date as month, sum(amount) as spent from fin.v_spend group by 1)
select {month_label("month")} as "Месяц", round(spent) as "Расходы бизнеса",
       round(avg(spent) over (order by month rows between 2 preceding and current row)) as "Среднее за 3 месяца"
from m order by month""", "combo",
         {"graph.dimensions": ["Месяц"], "graph.metrics": ["Расходы бизнеса", "Среднее за 3 месяца"],
          "series_settings": {"Расходы бизнеса": {"display": "bar", "color": BLUE},
                              "Среднее за 3 месяца": {"display": "line", "color": GREY}},
          "graph.show_values": True, "graph.x_axis.title_text": "", "graph.y_axis.title_text": "₽",
          **colfmt(**{"Расходы бизнеса": RUB, "Среднее за 3 месяца": RUB})},
         "Та же величина, что «Пульс → Деньги → Куда ушли деньги»: выписка без переводов себе + покупки «мимо "
         "счёта», включая вложения. Среднее — по доступным месяцам, не больше трёх.")
    card("r_cac", q, "CAC по месяцам и сценарии", f"""
select {month_label("period_start")} as "Месяц", spend_own as "Реклама", new_clients as "Новые клиенты", round(cac) as "CAC",
       round(first_order_after_cac) as "1-й заказ после CAC", round(after_cac_mid) as "При CAC 500"
from fin.v_cac where period_type = 'месяц' and new_clients is not null order by period_start""", "table",
         colfmt(**{"Реклама": RUB, "CAC": RUB, "1-й заказ после CAC": RUB, "При CAC 500": RUB}),
         "CAC = реклама своими + подрядчик / новые клиенты. «1-й заказ после CAC» — маржа первого заказа минус CAC; "
         "«При CAC 500» — та же маржа при сценарном CAC.")
    card("r_gates", q, "Ворота пилота", f"""
select gate as "Ворота", check_name as "Проверка",
       case when unit = '%' then fin.fmt_pct(value) else fin.fmt_rub(value) end as "Сейчас",
       case when unit = '%' then fin.fmt_pct(threshold) else fin.fmt_rub(threshold) end as "Порог",
       case status when 'зелёный' then 'пройдены' when 'красный' then 'не пройдены' else 'нет данных' end as "Статус",
       {d("period_end")} as "Срок" from fin.v_pilot_gates order by sort""", "table",
         color_rules("Статус", {"пройдены": GREEN, "не пройдены": RED}),
         "Неделя 2, неделя 6 и М3 пилота. Пороги — вводные «Ворота …».")
    number("r_stock_soon", q, "Склад", """
select 'Партия ещё не внесена — форма «Партия закупки»' where not exists (select 1 from fin.purchase_lots)""",
           "Склад появится после первой партии закупки.")
    card("r_stock", q, "Склад", """
select coalesce(n.display_name, s.product) as "Товар", s.on_hand as "На руках", round(s.days_left) as "Хватит, дней",
       case when s.order_now then 'пора заказывать' when s.reorder then 'мало' end as "Сигнал",
       s.grams_left as "Чая осталось, г", s.packed as "Расфасовано"
from fin.v_stock s left join fin.product_names n using (product)
where exists (select 1 from fin.purchase_lots)
order by s.order_now desc nulls last, s.reorder desc, s.kind, 1""", "table",
         color_rules("Сигнал", {"пора заказывать": RED, "мало": YELLOW}),
         "Точка заказа = (срок поставки + фасовка и маркировка + страховой запас) × продажи в день за 14 дней.")
    card("r_codes", q, "Коды «Честного знака»", """
select gtin as "GTIN", imported as "Из СУЗ", available as "Свободно", used_in_orders as "В заказах",
       in_circulation as "В обороте", written_off as "Списано" from fin.v_codes order by gtin""", "table", None,
         "Коды по GTIN: импортировано из СУЗ, свободно, в заказах, введено в оборот, списано.")


def cards_inputs(q):
    cols = ("Тема", "Вводная", "Значение", "С какой даты", "Вид", "Комментарий", "Ключ")
    card("i_inputs", q, "Вводные сейчас", """
select theme as "Тема", label as "Вводная", value_shown as "Значение", since as "С какой даты", kind as "Вид",
       comment as "Комментарий", key as "Ключ"
from fin.v_inputs_display where not unused order by theme, label""", "table",
         # Ширина колонок — явно: Metabase подбирает её по первым строкам, и «2. Комиссии» обрезалась.
         merge({"table.columns": [{"name": c, "enabled": c != "Ключ"} for c in cols],
                "table.column_widths": [130, 300, 130, 120, 110, 700]},
               colfmt(Тема={"column_title": "Тема"}),
               color_rules("Вид", {"допущение": YELLOW, "решение": BLUE})),
         "Действующие сегодня вводные по темам. Вид: факт — из документа или банка, решение — договорились, "
         "допущение — оценка, уточнить. Изменить — форма «Изменить вводную»; прежнее значение остаётся в истории ниже.")
    card("i_history", q, "История вводных", f"""
select regexp_replace(coalesce(label, key), '([А-Яа-яЁё])([0-9])', '\\1 \\2', 'g') as "Вводная",
       coalesce(replace(trim_scale(value)::text, '.', ','), value_text, 'нет данных') as "Значение",
       {d("valid_from")} as "С какой даты", entered_by as "Кто",
       to_char(created_at at time zone 'Europe/Moscow', 'DD.MM.YYYY HH24:MI') as "Записано",
       regexp_replace(concat_ws(' · ', status, note), '([А-Яа-яЁё])([0-9])', '\\1 \\2', 'g') as "Комментарий"
from fin.inputs where key in (select key from fin.inputs group by key having count(*) > 1)
order by coalesce(label, key), valid_from desc, created_at desc""", "table", None,
         "Вводные, которые меняли: все значения с датой, с которой они действуют.")


# Поля каталога в журнале — словами, как их видит владелец в форме.
CATALOG_FIELDS = {"price": "Цена", "on_sale": "В продаже", "active": "Действует", "name": "Название",
                  "description": "Описание", "brew_guide": "Как заваривать", "gtin": "GTIN",
                  "vk_market_url": "Маркет ВК", "recommend_with": "С чем советуем", "is_new": "Новинка",
                  "net_weight_g": "Фасовка, г", "raw_material": "Сырьё", "video_url": "Видео",
                  "synonyms": "Синонимы", "sort_order": "Порядок", "category": "Раздел", "kind": "Тип",
                  "label_name": "Этикетка: название", "label_kind": "Этикетка: вид продукции",
                  "storage": "Этикетка: хранение", "shelf_life_months": "Срок годности, мес",
                  "qr_url": "Этикетка: QR"}


def cards_catalog(q, models):
    field_names = " ".join(f"when '{k}' then '{v}'" for k, v in CATALOG_FIELDS.items())
    card("k_catalog", q, "Каталог", """
select c.sku_id as "Артикул", c.name as "Название", c.kind as "Тип", c.price as "Цена",
       case when c.on_sale then 'да' else 'нет' end as "В наличии",
       -- Хватает ли сырья, посчитает склад по партиям (следующий блок); до него — прочерк.
       '—' as "Хватает сырья",
       coalesce(c.gtin, '—') as "GTIN",
       case when c.is_new then 'да' else '' end as "Новинка",
       case when c.vk_market_url is null then 'нет' else 'есть' end as "Маркет ВК"
from shop.catalog c where c.active
order by c.on_sale desc, c.sort_order, c.name""", "table",
         merge(colfmt(Цена=RUB), color_rules("В наличии", {"да": GREEN, "нет": GREY}),
               {"column_settings": {'["name","Артикул"]': {
                   "click_behavior": {"type": "link", "linkType": "url",
                                      "linkTemplate": f"/model/{models['shop.catalog']}/{{{{Артикул}}}}"}}}}),
         "Что продаёт бот. Клик по артикулу — карточка со всеми полями и кнопкой «Исправить». "
         "Правка видна боту через несколько секунд; цена в заказах, уже оформленных, не меняется.")
    card("k_changes", q, "Последние изменения каталога", f"""
select to_char(h.changed_at at time zone 'Europe/Moscow', 'DD.MM.YYYY HH24:MI') as "Когда",
       h.sku_id as "Артикул",
       case when h.entity = 'состав' then 'Состав: ' || h.field
            when h.action = 'добавлен' then 'Добавлен'
            else case h.field {field_names} else h.field end end as "Что",
       left(h.old_value, 60) as "Было", left(h.new_value, 60) as "Стало",
       h.changed_by as "Кто", h.note as "Причина"
from shop.catalog_changes h order by h.id desc limit 20""", "table", None,
         "Каждая правка каталога — и формой, и в обход неё. Журнал ведёт база, исправить его нельзя.")
    card("k_bundles", q, "Состав сетов и наборов", """
select b.bundle_sku as "Набор", s.name as "Название", b.item_sku as "Вложение", i.name as "Что внутри",
       b.quantity as "Сколько", case when b.active then 'да' else 'убрано' end as "В составе"
from shop.bundle_items b join shop.catalog s on s.sku_id = b.bundle_sku join shop.catalog i on i.sku_id = b.item_sku
order by b.bundle_sku, b.active desc, b.item_sku""", "table", None,
         "Из чего собран каждый сет и набор. Добавить или убрать вложение — кнопка «Состав сета/набора».")


def catalog_forms(qcoll, fin_db, tables, models):
    """Модели каталога с «Исправить» (без «Удалить») и кнопки вкладки «Каталог»."""
    for table, title in (("shop.catalog", "Каталог"), ("shop.bundle_items", "Состав наборов")):
        body = {"name": f"Ввод: {title}", "display": "table", "visualization_settings": {}, "collection_id": qcoll,
                "type": "model", "dataset_query": {"type": "query", "database": fin_db,
                                                   "query": {"source-table": tables[table]}}}
        models[table] = call("POST", "/card", body)["id"]
        # Удаления нет: снять с продажи — кнопкой, убрать совсем — «Действует = нет».
        call("POST", "/action", {"name": f"Исправить: {title.lower()}", "type": "implicit", "kind": "row/update",
                                 "model_id": models[table]})
    rows = call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {"query": """
select sku_id || ' · ' || name, kind from shop.catalog where active order by sort_order, name"""}})["data"]["rows"]
    every = [r[0] for r in rows]
    bundles = [r[0] for r in rows if r[1] in ("сет", "набор")]
    packs = [r[0] for r in rows if r[1] in ("пачка", "блин")]
    # Артикул в списке — «код · название»; в SQL — код до точки.
    sku = "split_part({{%s}}, ' · ', 1)"
    fb.action("k_price", models["shop.catalog"], fin_db, "Изменить цену", f"""
update shop.catalog set price = {{{{price}}}}, change_note = {{{{reason}}}}, updated_by = {{{{entered_by}}}},
       price_confirmed = coalesce([[{{{{confirm}}}} = 'да' or ]] false)
where sku_id = {sku % 'sku'}
""", [fb.field("sku", "Артикул", "select", options=every), fb.field("price", "Новая цена, ₽", "number"),
      fb.field("reason", "Причина", "text"),
      fb.field("confirm", "Цена меняется больше чем на 30 % — «Да, цена верная»", "select", required=False,
               options=["да"]),
      fb.AUTHOR], "Цена записана — бот назовёт её через несколько секунд, в Ops ушло сообщение.")
    fb.action("k_add", models["shop.catalog"], fin_db, "Добавить артикул", f"""
insert into shop.catalog (sku_id, kind, name, net_weight_g, raw_material, price, description, brew_guide,
                          gtin, vk_market_url, category, updated_by, change_note)
values (upper({{{{sku_id}}}}), {{{{kind}}}}, {{{{name}}}}, {fb.OPT % 'weight'}::numeric, {fb.OPT % 'raw'},
        {fb.OPT % 'price'}::int, coalesce({fb.OPT % 'description'}, ''), coalesce({fb.OPT % 'brew'}, ''),
        {fb.OPT % 'gtin'}, {fb.OPT % 'vk'}, {fb.OPT % 'category'}, {{{{entered_by}}}}, {fb.OPT % 'note'})
""",
              [fb.field("sku_id", "Артикул (латиница, цифры, дефис: P-TGI-ECO-100)", "text"),
               fb.field("kind", "Тип", "select", options=["пачка", "блин", "сет", "набор"]),
               fb.field("name", "Название в боте (до 80 символов)", "text"),
               fb.field("weight", "Фасовка, г (у сета и набора — пусто)", "text", required=False),
               fb.field("raw", "Сырьё (у пачки и блина)", "select", required=False, options=fb.SKUS),
               fb.field("price", "Цена, ₽ (пусто — не продаётся отдельно)", "text", required=False),
               fb.field("description", "Описание", "textarea", required=False),
               fb.field("brew", "Как заваривать", "textarea", required=False),
               fb.field("gtin", "GTIN", "text", required=False),
               fb.field("vk", "Ссылка на Маркет ВК", "text", required=False),
               fb.field("category", "Раздел", "text", required=False),
               fb.NOTE, fb.AUTHOR],
              "Артикул добавлен, пока не в продаже: заполните GTIN и включите кнопкой «Снять с продажи / вернуть».")
    fb.action("k_sale", models["shop.catalog"], fin_db, "Снять с продажи / вернуть", f"""
update shop.catalog set on_sale = ({{{{state}}}} = 'в продаже'), change_note = {fb.OPT % 'note'},
       updated_by = {{{{entered_by}}}}
where sku_id = {sku % 'sku'}
""", [fb.field("sku", "Артикул", "select", options=every),
      fb.field("state", "Как теперь", "select", options=["в продаже", "снят с продажи"]),
      fb.NOTE, fb.AUTHOR], "Записано — бот узнает через несколько секунд, в Ops ушло сообщение.")
    fb.action("k_bundle", models["shop.bundle_items"], fin_db, "Состав сета/набора", f"""
insert into shop.bundle_items (bundle_sku, item_sku, quantity, active, updated_by, change_note)
values ({sku % 'bundle'}, {sku % 'item'}, coalesce([[{{{{quantity}}}}, ]] 1), {{{{op}}}} = 'добавить',
        {{{{entered_by}}}}, {fb.OPT % 'note'})
on conflict (bundle_sku, item_sku) do update
   set quantity = excluded.quantity, active = excluded.active,
       updated_by = excluded.updated_by, change_note = excluded.change_note
""", [fb.field("bundle", "Сет или набор", "select", options=bundles),
      fb.field("item", "Вложение (пачка или блин)", "select", options=packs),
      fb.field("quantity", "Сколько штук", "number", required=False),
      fb.field("op", "Что сделать", "select", options=["добавить", "убрать"]),
      fb.NOTE, fb.AUTHOR], "Состав записан.")
    return models


# Сырьё — названием без кода поставщика, как на «Товаре».
RAW = "coalesce(n.display_name, {0})"


def cards_stock(q):
    card("w_lots", q, "Партии сейчас", f"""
select {RAW.format('b.raw_material')} as "Сырьё", b.lot_code as "Партия", round(b.on_hand) as "Осталось",
       round(b.reserved) as "В резерве", round(b.free) as "Свободно", b.unit as "Ед.",
       floor(b.free / nullif(case when b.unit = 'шт' then 1 else r.main_pack end, 0)) as "Пачек основной фасовки",
       {d("b.best_before")} as "Годен до",
       round(b.free / nullif(r.daily_use, 0)) as "Хватит, дней",
       case when r.daily_from_sales then 'продажи 14 дн.' when r.daily_use is not null then 'план' end as "Расход по"
from shop.v_lot_balance b
left join fin.v_raw_stock r on r.raw_material = b.raw_material
left join fin.product_names n on n.product = b.raw_material
where b.active and b.on_hand > 0
order by 1, b.best_before nulls last""", "table", None,
         "Остаток партии — приход плюс движения журнала: резерв при оплате, списание при сборке, инвентаризация, "
         "порча. Свободно = остаток − резерв. Хватит — по средним продажам за 14 дней, до старта — по плану из вводных. "
         "Пустые партии скрыты.")
    card("w_reorder", q, "Пора докупать", f"""
select {RAW.format('o.raw_material')} as "Сырьё", round(o.free) || ' ' || o.unit as "Свободно",
       round(o.days_left) as "Хватит, дней", o.lead_days as "Поставка, дней",
       round(o.need) || ' ' || o.unit as "Нужно на срок", o.order_units || ' ' || o.order_unit as "Заказать"
from fin.v_reorder_raw o left join fin.product_names n on n.product = o.raw_material
where o.order_now order by o.days_left""", "table", None,
         "Свободного сырья меньше, чем уйдёт за срок поставки + фасовку + запас (вводные и «Сроки поставки»). "
         "Заказать — до кратности поставщика.")
    card("w_expiring", q, "Скоро истекает", f"""
select {RAW.format('e.raw_material')} as "Сырьё", e.lot_code as "Партия", {d("e.best_before")} as "Годен до",
       e.days_left as "Дней", round(e.on_hand) || ' ' || e.unit as "Осталось"
from fin.v_lots_expiring e left join fin.product_names n on n.product = e.raw_material order by e.best_before""",
         "table", None, "Партии, у которых до «годен до» меньше 90 дней.")
    card("w_diff", q, "Расхождения: инвентаризация и потери фасовки", f"""
select {d("x.created_at at time zone 'Europe/Moscow'")} as "Когда", x.lot_code as "Партия", x.kind as "Что",
       round(x.diff) || ' ' || x.unit as "Разница", round(100 * x.diff_share, 1) || ' %' as "От партии",
       x.created_by as "Кто", x.note as "Примечание"
from fin.v_stock_discrepancies x order by x.created_at desc limit 20""", "table", None,
         "Последние инвентаризации (факт минус учёт) и потери фасовки (перевес против номинала).")
    card("w_no_stock", q, "Включено, но нет сырья", """
select sku_id as "Артикул", name as "Название", kind as "Тип" from fin.v_on_sale_no_stock order by 1""", "table", None,
         "Владелец оставил в продаже, а сырья на единицу не хватает: бот такой артикул не продаёт. "
         "Внести партию или снять с продажи — решение владельца.")


def stock_forms(qcoll, fin_db, tables, models):
    """Партии сырья с «Исправить» и формы склада: приход, инвентаризация, порча."""
    body = {"name": "Ввод: Партии сырья", "display": "table", "visualization_settings": {}, "collection_id": qcoll,
            "type": "model", "dataset_query": {"type": "query", "database": fin_db,
                                               "query": {"source-table": tables["shop.lots"]}}}
    models["shop.lots"] = call("POST", "/card", body)["id"]
    call("POST", "/action", {"name": "Исправить: партия сырья", "type": "implicit", "kind": "row/update",
                             "model_id": models["shop.lots"]})
    lots = [r[0] for r in call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {"query": """
select b.lot_code || ' · ' || left(b.raw_material, 40) || ' · ' || trim_scale(round(b.on_hand))::text || ' ' || b.unit
from shop.v_lot_balance b where b.active order by b.raw_material, b.best_before nulls last"""}})["data"]["rows"]]
    lot = "(select id from shop.lots where lot_code = split_part({{%s}}, ' · ', 1))"
    fb.action("lot", models["shop.lots"], fin_db, "Партия закупки", f"""
insert into shop.lots (lot_code, raw_material, unit, qty, arrival_date, manufacture_date, best_before, manufacturer,
                       manufacturer_address, country, importer, supplier, supplier_declaration, invoice_ref,
                       price_per_unit, discount, delivery_cost, note, entered_by)
values ({{{{lot_code}}}}, {{{{raw}}}}, {{{{unit}}}}, {{{{qty}}}}, {{{{arrival}}}}::date,
        nullif([[{{{{made}}}} ||]] '', '')::date, nullif([[{{{{best_before}}}} ||]] '', '')::date,
        {fb.OPT % 'manufacturer'}, {fb.OPT % 'address'}, coalesce({fb.OPT % 'country'}, 'Китай'), {fb.OPT % 'importer'},
        {fb.OPT % 'supplier'}, {fb.OPT % 'declaration'}, {fb.OPT % 'invoice'}, {{{{price}}}},
        coalesce([[{{{{discount}}}} / 100.0, ]]0), coalesce([[{{{{delivery}}}}, ]]0), {fb.OPT % 'note'}, {{{{entered_by}}}})
""", [fb.field("lot_code", "Код партии (печатается на этикетке): L-ГГММДД-СОРТ", "text", placeholder="L-261101-TGY"),
      fb.field("raw", "Сырьё", "select", options=fb.SKUS),
      fb.field("unit", "Единица", "select", options=["г", "шт"]),
      fb.field("qty", "Количество (граммы рассыпного или штуки блинов)", "number"),
      fb.field("arrival", "Дата прихода", "date"),
      fb.field("made", "Дата производства", "date", required=False),
      fb.field("best_before", "Годен до (от производителя)", "date", required=False),
      fb.field("manufacturer", "Изготовитель (латиницей)", "text", required=False),
      fb.field("address", "Адрес изготовителя (латиницей)", "text", required=False),
      fb.field("country", "Страна (по умолчанию Китай)", "text", required=False),
      fb.field("importer", "Импортёр", "text", required=False),
      fb.field("supplier", "Поставщик", "text", required=False, placeholder="I-TEA"),
      fb.field("declaration", "Декларация поставщика (справочно)", "text", required=False),
      fb.field("invoice", "Номер счёта-фактуры", "text", required=False),
      fb.field("price", "Цена за кг или за штуку, ₽", "number"),
      fb.field("discount", "Скидка, %", "number", required=False),
      fb.field("delivery", "Доставка партии, ₽", "number", required=False),
      fb.NOTE, fb.AUTHOR], "Партия записана: бот видит сырьё сразу, себестоимость пересчитана по партиям.")
    fb.action("inventory", models["shop.lots"], fin_db, "Инвентаризация", f"""
insert into shop.stock_moves (lot_id, kind, qty, note, created_by)
select b.lot_id, 'корректировка', {{{{fact}}}} - b.on_hand,
       concat_ws(' · ', 'инвентаризация: факт ' || trim_scale({{{{fact}}}}::numeric)::text || ', по учёту '
                 || trim_scale(b.on_hand)::text, {fb.OPT % 'note'}), {{{{entered_by}}}}
from shop.v_lot_balance b where b.lot_id = {lot % 'lot'} and {{{{fact}}}} <> b.on_hand
""", [fb.field("lot", "Партия", "select", options=lots),
      fb.field("fact", "Факт на полке (г или шт)", "number"), fb.NOTE, fb.AUTHOR],
       "Записано: остаток партии — по факту, разница — в «Расхождениях».")
    fb.action("spoil", models["shop.lots"], fin_db, "Списать сырьё", f"""
insert into shop.stock_moves (lot_id, kind, qty, note, created_by)
values ({lot % 'lot'}, 'порча', {{{{qty}}}}, concat_ws(' · ', {{{{reason}}}}, {fb.OPT % 'note'}), {{{{entered_by}}}})
""", [fb.field("lot", "Партия", "select", options=lots), fb.field("qty", "Сколько (г или шт)", "number"),
      fb.field("reason", "Причина", "select", options=["порча", "просрочка", "брак", "другое"]), fb.NOTE, fb.AUTHOR],
       "Списано.")
    return models


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
from fin.v_bank_ops order by day desc limit 200"""}})["data"]["rows"]
    items = [r[0] for r in call("POST", "/dataset", {"database": fin_db, "type": "native", "native": {
        "query": "select item from fin.budget_articles order by sort"}})["data"]["rows"]]
    # Операция, оплатившая плановый расход, закрывает его; статья — та же, что у плана, если не выбрана.
    fb.action("mark", models["bank_marks"], fin_db, "Разметить операцию", f"""
insert into fin.bank_marks (operation_id, transfer_kind, budget_item, partner, closes_plan_id, note, entered_by)
select b.operation_id, {fb.OPT % 'kind'},
       coalesce({fb.OPT % 'item'}, (select p.budget_item from fin.v_plans p where p.id = {fb.PLAN_ID % 'closes'})),
       {fb.OPT % 'partner'}, {fb.PLAN_ID % 'closes'}, {fb.OPT % 'note'}, {{{{entered_by}}}}
from (select distinct operation_id from fin.v_bank_ops where left(operation_id, 8) = right({{{{op}}}}, 8)) b
""", [fb.field("op", "Операция (дата · сумма · статья · код)", "select", options=[r[0] for r in ops]),
      fb.field("kind", "Если это перевод себе — что это", "select", required=False,
               options=["на покупки", "вывод владельцем", "между своими счетами"]),
      fb.field("item", "Статья бюджета пилота", "select", required=False, options=items),
      fb.field("partner", "Если это взнос — чей вклад", "select", required=False, options=fb.AUTHORS),
      fb.field("closes", "Закрывает план (операция оплатила плановый расход)", "select", required=False,
               options=fb.open_plans(fin_db)),
      fb.NOTE, fb.AUTHOR], "Операция размечена — расходы, вклады, ДДС и сверка покупок с карты пересчитаны.")
    return models


# --- раскладка --------------------------------------------------------------------------

_seq = [0]


def dc(key, tab, row, col, w, h, hide_empty=False, title=None):
    _seq[0] -= 1
    viz = {"card.hide_empty": True} if hide_empty else {}
    if title is not None:
        viz["card.title"] = title
    return {"id": _seq[0], "card_id": CARDS[key], "dashboard_tab_id": tab, "row": row, "col": col,
            "size_x": w, "size_y": h, "parameter_mappings": [], "visualization_settings": viz}


def text(tab, row, col, w, h, md):
    _seq[0] -= 1
    return {"id": _seq[0], "card_id": None, "dashboard_tab_id": tab, "row": row, "col": col, "size_x": w, "size_y": h,
            "parameter_mappings": [],
            "visualization_settings": {"text": md, "dashcard.background": False, "virtual_card": {"name": None, "display": "text", "archived": False,
                                                                     "visualization_settings": {}, "dataset_query": {}}}}


def button(key, label, tab, row, col, w=4, h=1):
    _seq[0] -= 1
    return {"id": _seq[0], "card_id": None, "action_id": fb.ACTIONS[key], "dashboard_tab_id": tab,
            "row": row, "col": col, "size_x": w, "size_y": h, "parameter_mappings": [],
            "visualization_settings": {"button.label": label, "actionDisplayType": "button",
                                       "virtual_card": {"display": "action", "visualization_settings": {},
                                                        "dataset_query": {}, "archived": False, "name": None}}}


def rows_height(sql, *, per_row=0.5, extra=1.5, low=2, high=12):
    """Высота таблицы под число строк: строки ≈ полклетки сетки, плюс заголовок."""
    n = one_row(f"select count(*) from ({sql}) x")[0]
    return max(low, min(high, round(extra + per_row * int(n) + 0.49)))


# Фильтр дашборда «Период»: FCF по месяцам или по неделям.
GRAIN_PARAM = {"id": "grain", "name": "Период", "slug": "period", "type": "string/=", "sectionId": "string",
               "default": ["месяц"], "values_query_type": "list", "values_source_type": "static-list",
               "values_source_config": {"values": ["месяц", "неделя"]}}
GRAIN_CARDS = ("m_fcf", "m_fcf_cum")


def put(did, tabs, dashcards, parameters=()):
    for dashcard in dashcards:
        for key in GRAIN_CARDS:
            if parameters and dashcard.get("card_id") == CARDS.get(key):
                dashcard["parameter_mappings"] = [{"parameter_id": "grain", "card_id": CARDS[key],
                                                   "target": ["variable", ["template-tag", "grain"]]}]
    call("PUT", f"/dashboard/{did}", {"tabs": [{"id": tid, "name": name} for name, tid in tabs.items()],
                                      "dashcards": dashcards, "parameters": list(parameters)})


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
        tables = fb.form_tables(fin_db)
    if "bank_marks" not in tables:
        raise SystemExit("Metabase не видит fin.bank_marks: миграции 009–010 применены (analytics/fin/apply.sh)?")
    root, qcoll, old = collections()
    pulse, ptabs = ensure_dashboard(root, PULSE, "Каждый день: всё ли в порядке, куда уходят деньги, работают ли реклама "
                                    "и бот, на чём зарабатываем. Раз в неделю и в конце месяца: ввод данных, сверки, "
                                    "отчёты, вводные, каталог. Выписка — 07:00 и 21:00 МСК, заказы — сразу.", PULSE_TABS)
    links = {name: f"{SITE}/dashboard/{pulse}?tab={ptabs[tab]}"
             for name, tab in (("money", "Деньги"), ("sales", "Продажи"), ("entry", "Ввод данных"), ("checks", "Сверки"),
                               ("stock", "Склад"))}
    for item, color, cls in call("POST", "/dataset", {"database": RO_DB, "type": "native", "native": {
            "query": "select item, color, cash_class from fin.budget_articles"}})["data"]["rows"]:
        SERIES_COLORS[SHORT_ITEMS.get(item, item) + (" (влож.)" if cls == "Вложения" else "")] = {"color": color}

    models = build_forms(qcoll, fin_db, tables)
    catalog_forms(qcoll, fin_db, tables, models)
    stock_forms(qcoll, fin_db, tables, models)
    cards_today(qcoll, links)
    cards_money(qcoll)
    cards_sales(qcoll)
    cards_product(qcoll)
    cards_entry(qcoll)
    cards_checks(qcoll)
    cards_reports(qcoll)
    cards_inputs(qcoll)
    cards_catalog(qcoll, models)
    cards_stock(qcoll)

    budget_h = rows_height("select * from fin.v_pilot_budget_status where not (coalesce(plan, 0) = 0 "
                           "and coalesce(spent, 0) = 0 and coalesce(planned, 0) = 0)", extra=2.5)
    ahead_h = rows_height("select * from fin.v_payments_ahead", extra=2.5, low=3, high=8)
    lots_h = rows_height("select 1 from shop.v_lot_balance where active and on_hand > 0", extra=2.5, low=3, high=12)
    att_h = rows_height("select * from fin.v_attention", per_row=0.75, extra=1.5, low=3, high=8)
    T, M, S, P, W = ptabs["Сегодня"], ptabs["Деньги"], ptabs["Продажи"], ptabs["Товар"], ptabs["Склад"]
    E, C, R, I, K = ptabs["Ввод данных"], ptabs["Сверки"], ptabs["Отчёты"], ptabs["Вводные"], ptabs["Каталог"]
    # Скрытые без данных карточки Metabase убирает из сетки, и всё ниже поднимается: «убыток
    # пилота» после старта встаёт на место «дней до старта», строка «без вкладов» пропадает,
    # когда капитал внесён.
    put(pulse, ptabs, parameters=[GRAIN_PARAM], dashcards=[
        dc("t_free", T, 0, 0, 6, 4), dc("t_capital", T, 0, 6, 6, 4),
        dc("t_pilot", T, 0, 12, 6, 4, hide_empty=True), dc("t_loss", T, 0, 18, 6, 4, hide_empty=True),
        # «Денег хватит до» — строкой во всю ширину, как прогноз без вкладов под ним: текст
        # «на весь прогноз (до 31.03.2027)» в плитке обрезался — шрифт растёт вместе с ней.
        dc("t_runway", T, 4, 0, 24, 2),
        dc("t_nocap", T, 6, 0, 24, 2, hide_empty=True),
        dc("t_attention", T, 8, 0, 24, att_h),
        dc("t_balance", T, 8 + att_h, 0, 24, 7),
        dc("t_soon", T, 15 + att_h, 0, 24, 2, hide_empty=True),
        # Деньги
        dc("m_spend", M, 0, 0, 12, 8),
        dc("m_partners", M, 0, 12, 12, rows_height("select * from fin.v_partner_capital", extra=2.5)),
        dc("m_forecast", M, 4, 12, 12, 6),
        dc("m_budget", M, 10, 0, 24, budget_h),
        # Платежи впереди и FCF — под бюджетом.
        dc("m_ahead_total", M, 10 + budget_h, 0, 6, 3), dc("m_ahead", M, 10 + budget_h, 6, 18, ahead_h),
        dc("m_fcf", M, 10 + budget_h + ahead_h, 0, 14, 7), dc("m_fcf_cum", M, 10 + budget_h + ahead_h, 14, 10, 7),
        # Продажи: до первой оплаты — одна строка, что здесь будет.
        dc("s_soon", S, 0, 0, 24, 2, hide_empty=True),
        dc("s_funnel", S, 2, 0, 12, 7, True), dc("s_daily", S, 2, 12, 12, 7, True),
        dc("s_cac", S, 9, 0, 12, 6, True), dc("s_check", S, 9, 12, 12, 6, True),
        # Товар
        dc("p_waterfall", P, 0, 0, 24, 7),
        dc("p_margin", P, 7, 0, 12, 11), dc("p_breakeven", P, 7, 12, 12, 4), dc("p_scenarios", P, 11, 12, 12, 4),
        dc("p_sales", P, 18, 0, 24, 6, True),
        # Склад
        button("lot", "Партия закупки", W, 0, 0, 6), button("inventory", "Инвентаризация", W, 0, 6, 6),
        button("spoil", "Списать сырьё", W, 0, 12, 6),
        dc("w_lots", W, 1, 0, 24, lots_h),
        dc("w_reorder", W, 1 + lots_h, 0, 12, 5), dc("w_expiring", W, 1 + lots_h, 12, 12, 5),
        dc("w_no_stock", W, 6 + lots_h, 0, 12, 5), dc("w_diff", W, 6 + lots_h, 12, 12, 5),
        text(E, 0, 0, 24, 1, "**Каждый день**"),
        button("ads", "Реклама за день", E, 1, 0), button("expense", "Добавить расход", E, 1, 4),
        text(E, 2, 0, 24, 1, "**Редко**"),
        button("capital", "Вклад / выплата партнёра", E, 3, 0), button("snapshot", "Остаток по выписке", E, 3, 4),
        button("lot", "Партия закупки", E, 3, 8), button("inventory", "Инвентаризация", E, 3, 12),
        button("input", "Изменить вводную", E, 3, 16), button("mark", "Разметить операцию", E, 3, 20),
        button("act", "Акт перевозчика", E, 4, 0), button("lead", "Срок поставки", E, 4, 4),
        button("writeoff", "Списать код ЧЗ", E, 4, 8),
        dc("e_recent", E, 5, 0, 24, 8),
        dc("c_snapshot", C, 0, 0, 12, 3), dc("c_card", C, 0, 12, 12, 3),
        dc("c_ops", C, 3, 0, 24, 8), dc("c_claude", C, 11, 0, 24, 7), dc("c_yk", C, 18, 0, 24, 5, True),
        dc("c_uncl", C, 23, 0, 12, 5, True), dc("c_holds", C, 23, 12, 12, 5, True),
        dc("r_pnl", R, 0, 0, 24, 9), dc("r_ddc", R, 9, 0, 24, 6),
        dc("r_flow", R, 15, 0, 12, 7), dc("r_spent", R, 15, 12, 12, 7),
        dc("r_cac", R, 22, 0, 24, 4, True), dc("r_gates", R, 26, 0, 24, 6),
        dc("r_stock_soon", R, 32, 0, 24, 2, True), dc("r_stock", R, 34, 0, 14, 8, True),
        dc("r_codes", R, 34, 14, 10, 8, True),
        dc("i_inputs", I, 0, 0, 24, 16), dc("i_history", I, 16, 0, 24, 6),
        button("k_price", "Изменить цену", K, 0, 0, 6), button("k_add", "Добавить артикул", K, 0, 6, 6),
        button("k_sale", "Снять с продажи / вернуть", K, 0, 12, 6), button("k_bundle", "Состав сета/набора", K, 0, 18, 6),
        dc("k_catalog", K, 1, 0, 24, rows_height("select 1 from shop.catalog where active", extra=2.5, high=14)),
        dc("k_changes", K, 15, 0, 14, 9), dc("k_bundles", K, 15, 14, 10, 9),
    ])

    failed = verify()
    call("PUT", f"/dashboard/{pulse}", {"collection_position": 1})
    call("PUT", "/setting/custom-homepage", {"value": True})
    call("PUT", "/setting/custom-homepage-dashboard", {"value": pulse})
    for c in old:
        call("PUT", f"/collection/{c['id']}", {"archived": True})
    if not failed:
        archive_old(root)
    print(f"{PULSE}: {SITE}/dashboard/{pulse}")
    raise SystemExit(1 if failed else 0)
