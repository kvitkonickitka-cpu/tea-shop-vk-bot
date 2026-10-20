"""Схема `analytics` — договор с Yandex DataLens.

Чарты DataLens смотрят только сюда, а не в рабочие таблицы: рабочие
меняются вместе с ботом, а здесь **столбцы только добавляются** — новые в
конец, существующие не переименовываются и не удаляются. Postgres сам
держит это правило: `CREATE OR REPLACE VIEW` не даст убрать столбец или
сменить его тип, а тест `tests/test_analytics_contract.py` сверяет
представления с `analytics/contract.yaml`.

Во всех представлениях:
- нет ФИО, телефонов, почт, адресов и текстов переписки; клиент — только
  псевдонимный `client_key` (HMAC от VK ID, `app/core/client_key.py`);
- тестовые данные исключены: тестовые аккаунты и заказы (`is_test`);
- суммы — numeric в рублях; время — timestamptz (UTC) и рядом дата по Москве.

Описания столбцов уходят в `COMMENT ON`: по ним ИИ-помощник DataLens
понимает, что где лежит. Поэтому описание живёт рядом с выражением.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SCHEMA = "analytics"
# Рекомендательная блокировка на пересоздание: контейнеры стартуют
# параллельно, а одновременный CREATE OR REPLACE одного представления
# падает на «tuple concurrently updated».
_LOCK_KEY = 7_406_101


def msk_date(column: str) -> str:
    return f"({column} at time zone 'Europe/Moscow')::date"


@dataclass(frozen=True)
class Column:
    name: str
    expr: str
    comment: str


@dataclass(frozen=True)
class View:
    name: str
    comment: str
    columns: tuple[Column, ...]
    body: str  # FROM … WHERE … (и WITH, если нужен, — в `prefix`)
    prefix: str = ""

    def create_sql(self) -> str:
        select = ",\n  ".join(f"{c.expr} as {c.name}" for c in self.columns)
        return f"create or replace view {SCHEMA}.{self.name} as\n{self.prefix}select\n  {select}\n{self.body}"

    def comment_sql(self) -> list[str]:
        statements = [f"comment on view {SCHEMA}.{self.name} is {_quote(self.comment)}"]
        statements += [
            f"comment on column {SCHEMA}.{self.name}.{c.name} is {_quote(c.comment)}" for c in self.columns
        ]
        return statements


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _values(rows: list[tuple]) -> str:
    def literal(value) -> str:
        if value is None:
            return "null"
        if isinstance(value, int):
            return str(value)
        return _quote(str(value))

    return ",\n    ".join("(" + ", ".join(literal(v) for v in row) + ")" for row in rows)


# --- справочники -------------------------------------------------------------

# Статусы заказа (`orders.status`): код, подпись, порядок по пути заказа.
STATUSES = [
    ("awaiting_payment", "Ждёт оплаты", 10),
    ("payment_failed", "Счёт не выставился", 15),
    ("payment_expired", "Счёт истёк", 20),
    ("canceled", "Отменён клиентом", 25),
    ("paid", "Оплачен", 30),
    ("confirmed", "Оплачен, передаётся перевозчику", 40),
    ("cdek_registered", "Заведён в СДЭК", 50),
    ("cdek_stuck", "СДЭК не ответил", 55),
    ("cdek_rejected", "СДЭК отказал", 56),
    ("reported", "Передан в работу", 60),
    ("refunded", "Возврат", 90),
]

# События воронки: код, подпись, порядок в воронке (NULL — вне основной
# воронки), этап. Нажатия кнопок и касания — общим кодом `button` / `touch`
# и отдельными строками для тех, что важны сами по себе.
EVENTS = [
    ("dialog_start", "Начало диалога", 10, "Диалог"),
    ("consult_started", "Консультация начата", 12, "Диалог"),
    ("consult_qualified", "Консультация с товаром из каталога", 15, "Диалог"),
    ("take_shown", "Показаны кнопки «Взять»", 20, "Диалог"),
    ("button:take", "Нажата «Взять»", 25, "Диалог"),
    ("draft_created", "Черновик создан", 30, "Заказ"),
    ("upsell_offered", "Допродажа предложена", 35, "Заказ"),
    ("upsell_accepted", "Допродажа принята", 36, "Заказ"),
    ("delivery_quoted", "Доставка посчитана", 40, "Заказ"),
    ("point_chosen", "Пункт выдачи выбран", 45, "Заказ"),
    ("recipient_set", "Получатель записан", 50, "Заказ"),
    ("invoice_auto", "Счёт выставлен автоматически", 60, "Счёт"),
    ("invoice_confirmed", "Счёт после «Оформляем?»", 60, "Счёт"),
    ("invoice_returning", "Счёт постоянному клиенту", 60, "Счёт"),
    ("invoice_repeat", "Счёт по «Повторить»", 60, "Счёт"),
    ("invoice_manual", "Счёт командой менеджера", 60, "Счёт"),
    ("payment_reminder_1", "Напоминание об оплате 1", 65, "Счёт"),
    ("payment_reminder_2", "Напоминание об оплате 2", 66, "Счёт"),
    ("invoice_expired", "Счёт истёк", None, "Счёт"),
    ("invoice_canceled", "Счёт отменён магазином", None, "Счёт"),
    ("payment_declined", "Банк отказал", None, "Счёт"),
    ("payment_succeeded", "Оплата прошла", 70, "Оплата"),
    ("shipment_created", "Отправление создано", 80, "Доставка"),
    ("handed_over", "Передано перевозчику", 85, "Доставка"),
    ("at_pickup_point", "В пункте выдачи", 88, "Доставка"),
    ("delivered", "Вручено", 90, "Доставка"),
    ("not_delivered", "Не вручено", None, "Доставка"),
    ("refunded", "Возврат", None, "После покупки"),
    ("canceled_by_client", "Отмена клиентом", None, "Заказ"),
    ("rated", "Оценка", 95, "После покупки"),
    ("review_saved", "Отзыв", 96, "После покупки"),
    ("escalation_opened", "Вопрос менеджеру открыт", None, "Менеджер"),
    ("escalation_closed", "Менеджер ответил", None, "Менеджер"),
    ("opted_out", "Отписка «стоп»", None, "После покупки"),
    ("button", "Нажатие кнопки", None, "Кнопки"),
    ("button_stale", "Нажата устаревшая кнопка", None, "Кнопки"),
    ("touch", "Повторное касание отправлено", None, "Касания"),
    ("touch_order", "Заказ после касания (7 дней)", None, "Касания"),
    ("touch_optout", "Отписка после касания (2 дня)", None, "Касания"),
    ("delivery_upgrade", "Выбрана доставка быстрее и дороже", None, "Заказ"),
    ("storefront_vk_delivery", "Способ доставки, выбранный в «Товарах»", None, "Заказ"),
    ("geo_button_shown", "Показана кнопка геопозиции", None, "Заказ"),
    ("geo_sent", "Геопозиция отправлена", None, "Заказ"),
    ("invoice_single_point", "Счёт сразу: на улице один пункт", None, "Счёт"),
    ("recipient_one_message", "Получатель записан кодом из одного сообщения", None, "Заказ"),
    ("payment_method_switch_offered", "После отказа банка — сразу новая ссылка", None, "Счёт"),
    ("escalation_after_hours", "Вопрос менеджеру вне рабочего времени", None, "Менеджер"),
    ("abandoned_touch", "Касание по брошенному заказу (~20 ч)", None, "Касания"),
    ("abandoned_touch_order", "Оплата после касания по брошенному заказу (7 дней)", None, "Касания"),
    ("pickup_reminder_1", "Напоминание: посылка в пункте (2-й день)", None, "Доставка"),
    ("pickup_reminder_2", "Напоминание: завтра последний день хранения", None, "Доставка"),
    ("free_delivery_nudge_shown", "Подсказка до бесплатной доставки показана", None, "Заказ"),
    ("free_delivery_nudge_accepted", "Пачка из подсказки добавлена", None, "Заказ"),
]

CHANNELS = {
    "dialog": "Диалог",
    "storefront": "Витрина ВК",
    "returning": "Постоянный клиент",
    "repeat": "Повтор заказа",
}

SOURCES = {
    "text": "Текст клиента",
    "button": "Кнопка",
    "code": "Код бота",
    "storefront": "Витрина ВК",
    "reminder": "Напоминание",
    "manager": "Менеджер",
    "carrier": "Перевозчик",
    "yookassa": "ЮKassa",
}

RATINGS = {"great": "Очень понравился", "ok": "Нормально", "no": "Не моё"}

TOUCHES = {
    "feedback_ask": "Оценка",
    "repeat_nudge": "«Повторить заказ?»",
    "second_touch": "Второй шанс",
    "reactivation": "Реактивация",
}


def _case(column: str, labels: dict[str, str]) -> str:
    whens = " ".join(f"when {_quote(code)} then {_quote(label)}" for code, label in labels.items())
    return f"case {column} {whens} else {column} end"


DIM_STATUS = View(
    name="dim_status",
    comment="Справочник статусов заказа: код из orders.status, подпись по-русски, порядок по пути заказа.",
    columns=(
        Column("code", "code::text", "Код статуса, как в v_orders.status."),
        Column("label", "label::text", "Подпись статуса по-русски."),
        Column("funnel_order", "funnel_order::int", "Порядок статуса на пути заказа: меньше — раньше."),
    ),
    body=f"from (values\n    {_values(STATUSES)}\n) as s(code, label, funnel_order)",
)

DIM_EVENT = View(
    name="dim_event",
    comment=(
        "Справочник событий воронки: код из v_funnel_events.event_kind или event, подпись "
        "по-русски, порядок в воронке и этап."
    ),
    columns=(
        Column("code", "code::text", "Код события. button и touch — общие коды для всех кнопок и касаний."),
        Column("label", "label::text", "Подпись события по-русски."),
        Column(
            "funnel_order", "funnel_order::int",
            "Порядок шага в основной воронке от первого сообщения до отзыва; пусто — событие вне воронки.",
        ),
        Column("stage", "stage::text", "Этап: Диалог, Заказ, Счёт, Оплата, Доставка, После покупки, Менеджер, Кнопки, Касания."),
    ),
    body=f"from (values\n    {_values(EVENTS)}\n) as e(code, label, funnel_order, stage)",
)

# Источник клиента (app/modules/dialog/sessions.py): ad:<ref>, referral:<код>,
# organic, own. Метка «свои» (/own) перекрывает и первый источник, и источник
# сессии. У клиентов до 10.2026 first_source пуст — выводим из ref.
def _first_source(c: str = "c") -> str:
    return (f"case when {c}.own_at is not null then 'own' else coalesce({c}.first_source, "
            f"case when nullif({c}.ref, '') is not null then 'ad:' || {c}.ref else 'organic' end) end")


def _session_source(s: str = "s", c: str = "c") -> str:
    return f"case when {c}.own_at is not null then 'own' else coalesce({s}.source, 'organic') end"


def _kind(expr: str) -> str:
    return f"split_part({expr}, ':', 1)"


SOURCE_KINDS = {"ad": "Реклама", "referral": "Рекомендация друга", "organic": "Органика", "own": "Свои"}

# Тестовые данные: тестовый аккаунт или тестовый заказ.
_REAL_ORDER = "not o.is_test and not coalesce(c.is_test, false)"
_PAID = "o.payment_status = 'succeeded'"

V_ORDERS = View(
    name="v_orders",
    comment=(
        "Заказы клиентов, по строке на заказ, без тестовых. Заказ появляется, когда выставлен счёт "
        "(или оформлен заказ без оплаты в старой схеме); черновики сюда не попадают. "
        "Оплаченный — is_paid; выручку считать по total оплаченных, без возвращённых (is_refunded)."
    ),
    prefix=(
        "with attempts as (\n"
        "  select order_id, count(*) as n,\n"
        "         max(payment_method) filter (where status = 'succeeded') as method,\n"
        "         sum(income_amount) filter (where status = 'succeeded' and refund_id is null) as income,\n"
        "         min(updated_at) filter (where status = 'succeeded') as paid_at\n"
        "  from order_payments group by order_id\n"
        "), invoices as (\n"
        "  select order_id,\n"
        "         bool_or(event = 'invoice_repeat') as repeat,\n"
        "         bool_or(event = 'invoice_returning') as returning\n"
        "  from funnel_events where event in ('invoice_repeat', 'invoice_returning') and order_id is not null\n"
        "  group by order_id\n"
        ")\n"
    ),
    columns=(
        Column("order_id", "o.id", "Номер заказа — тот же, что видят клиент и менеджер."),
        Column("client_key", "c.client_key", "Псевдонимный ключ клиента (HMAC от VK ID). Тот же в остальных представлениях."),
        Column(
            "client_order_number", "row_number() over (partition by o.peer_id order by o.created_at, o.id)::int",
            "Порядковый номер заказа у клиента среди всех его заказов: 1 — первый.",
        ),
        Column(
            "client_paid_order_number",
            f"case when {_PAID} then row_number() over (partition by o.peer_id, {_PAID} "
            "order by o.created_at, o.id) end::int",
            "Порядковый номер среди оплаченных заказов клиента: 1 — первая покупка, 2 и больше — повторная. "
            "У неоплаченных пусто.",
        ),
        Column("created_at", "o.created_at", "Когда заказ создан (выставлен первый счёт), UTC."),
        Column("created_date_msk", msk_date("o.created_at"), "Дата создания заказа по Москве."),
        Column(
            "paid_at",
            f"case when {_PAID} then coalesce(o.paid_at, a.paid_at, pn.sent_at) end",
            "Когда пришли деньги, UTC. У старых заказов без отметки — время сообщения клиенту об оплате.",
        ),
        Column("paid_date_msk", msk_date(f"case when {_PAID} then coalesce(o.paid_at, a.paid_at, pn.sent_at) end"),
               "Дата оплаты по Москве."),
        Column("delivered_at", "o.delivered_at", "Когда посылку вручили клиенту, UTC (по данным перевозчика или отметке менеджера)."),
        Column("delivered_date_msk", msk_date("o.delivered_at"), "Дата вручения по Москве."),
        Column("status", "o.status", "Статус заказа кодом; подпись — status_label, справочник — dim_status."),
        Column("status_label", "coalesce(ds.label, o.status)", "Статус заказа по-русски."),
        Column("is_paid", f"coalesce({_PAID}, false)", "Деньги по заказу пришли (в том числе если потом был возврат)."),
        Column("is_refunded", "o.status = 'refunded'", "По заказу оформлен возврат."),
        Column("is_canceled", "o.status = 'canceled'", "Клиент отменил заказ до оплаты."),
        Column("is_delivered", "o.delivered_at is not null", "Посылка вручена."),
        Column(
            "channel",
            "case when o.details ? 'vk_order_id' or o.details->>'origin' = 'storefront' then 'storefront'"
            " when o.details->>'origin' = 'repeat' or coalesce(i.repeat, false) then 'repeat'"
            " when o.details->>'origin' = 'returning' or coalesce(i.returning, false) then 'returning'"
            " else 'dialog' end",
            "Канал заказа кодом: dialog — собран в переписке, storefront — из витрины ВК, returning — "
            "постоянный клиент («как в прошлый раз»), repeat — «Повторить» (обычно по напоминанию).",
        ),
        Column(
            "channel_label",
            _case(
                "case when o.details ? 'vk_order_id' or o.details->>'origin' = 'storefront' then 'storefront'"
                " when o.details->>'origin' = 'repeat' or coalesce(i.repeat, false) then 'repeat'"
                " when o.details->>'origin' = 'returning' or coalesce(i.returning, false) then 'returning'"
                " else 'dialog' end",
                CHANNELS,
            ),
            "Канал заказа по-русски.",
        ),
        Column(
            "carrier",
            "case when o.delivery_method like 'cdek%' then 'cdek' when o.delivery_method like 'ozon%' then 'ozon'"
            " when o.delivery_method = 'russian_post' then 'russian_post' end",
            "Перевозчик: cdek, ozon, russian_post.",
        ),
        Column("delivery_method", "o.delivery_method", "Способ доставки кодом: ozon_pvz, cdek_pvz, cdek_courier, russian_post."),
        Column(
            "delivery_type",
            "case when o.delivery_method like '%courier' then 'courier' when o.delivery_method like '%pvz' then 'pickup'"
            " when o.delivery_method = 'russian_post' then 'post' end",
            "Тип доставки: pickup — пункт выдачи, courier — курьер, post — почта.",
        ),
        Column("items_total", "o.items_total::numeric(10,2)", "Сумма товаров, руб."),
        Column("delivery_cost", "coalesce(o.delivery_cost, 0)::numeric(10,2)", "Доставка для клиента, руб. 0 — бесплатная."),
        Column(
            "carrier_delivery_cost",
            "coalesce((o.details->>'carrier_delivery_cost')::numeric, o.delivery_cost)::numeric(10,2)",
            "Цена перевозчика, руб.: при бесплатной доставке её платит магазин.",
        ),
        Column("total", "o.total::numeric(10,2)", "Итог к оплате клиентом, руб.: товары + доставка для клиента."),
        Column(
            "free_delivery",
            "(o.details ? 'carrier_delivery_cost' and coalesce(o.delivery_cost, 0) = 0)",
            "Доставка бесплатна для клиента: заказ выше порога FREE_DELIVERY_THRESHOLD.",
        ),
        Column(
            "items_count",
            "(select coalesce(sum((it->>'quantity')::int), 0) from jsonb_array_elements(o.items) it)::int",
            "Сколько пачек в заказе.",
        ),
        Column("upsell_offered", "coalesce((o.details->>'upsell_offered')::boolean, false)",
               "Клиенту предлагали допродажу из столбца «С чем советуем»."),
        Column("upsell_item", "o.details->>'upsell_item'", "Какой товар предлагали допродажей."),
        Column(
            "upsell_accepted",
            "coalesce(o.details->>'upsell_item' is not null and exists (select 1 from jsonb_array_elements(o.items) it"
            " where it->>'name' = o.details->>'upsell_item'), false)",
            "Предложенный допродажей товар есть в заказе.",
        ),
        Column(
            "payment_attempts",
            "coalesce(a.n, case when o.payment_id is not null then 1 else 0 end)::int",
            "Сколько раз выставляли счёт по заказу: второй и дальше — после истёкшего счёта, отказа банка или правки.",
        ),
        Column("payment_method", "a.method", "Способ оплаты от ЮKassa: bank_card, sbp, yoo_money и т. п. У старых заказов пусто."),
        Column("income_amount", "a.income::numeric(10,2)", "Сколько получит магазин после комиссии ЮKassa, руб. Есть не у всех заказов."),
        Column("rating", "r.rating", "Оценка клиента кодом: great, ok, no. Пусто — не оценивал."),
        Column("rating_label", _case("r.rating", RATINGS), "Оценка по-русски: «Очень понравился», «Нормально», «Не моё»."),
        Column("has_review", "f.order_id is not null", "Клиент оставил отзыв о заказе."),
        Column("ref", "c.ref", "Метка рекламной кампании клиента (ref из ссылки при первом контакте). Пусто — органика."),
        Column("ref_source", "c.ref_source", "Источник метки кампании (ref_source). Пусто — органика."),
        Column(
            "expected_delivery_date", "(o.details->>'expected_by')::date",
            "Ожидаемая дата получения, названная клиенту на момент счёта (поздняя граница). Пусто — "
            "перевозчик не отдал срок или заказ старше 04.10.2026. Сравнивать с delivered_date_msk.",
        ),
        Column(
            "delivery_surcharge", "coalesce((o.details->>'delivery_surcharge')::numeric, 0)::numeric(10,2)",
            "Доплата клиента за перевозчика быстрее бесплатного (выше порога), руб. 0 — без доплаты.",
        ),
        Column("dialog_id", "sess.id", "Сессия диалога, в которой выставлен счёт (v_dialogs.dialog_id)."),
        Column("first_source", _first_source(),
               "Первый источник клиента (first touch): ad:<метка>, referral:<код>, organic, own («свои»)."),
        Column("first_source_kind", _kind(_first_source()), "Вид первого источника: ad, referral, organic, own."),
        Column("session_source", _session_source("sess"),
               "Источник сессии, в которой выставлен счёт (last touch): ad:<метка>, referral:<код>, organic, own."),
        Column("session_source_kind", _kind(_session_source("sess")), "Вид источника сессии: ad, referral, organic, own."),
        Column("carrier_actual_cost", "(o.details->>'carrier_actual_cost')::numeric(10,2)",
               "Фактическая стоимость доставки у перевозчика, руб. (СДЭК по API). Пусто — факта нет, см. акты в fin."),
        Column("carrier_track", "coalesce(o.details->>'cdek_number', o.ozon_posting)",
               "Номер отправления у перевозчика: накладная СДЭК или отправление Ozon — для сверки с актами."),
    ),
    body=(
        "from orders o\n"
        "left join clients c on c.peer_id = o.peer_id\n"
        "left join attempts a on a.order_id = o.id\n"
        "left join invoices i on i.order_id = o.id\n"
        "left join client_notices pn on pn.ref = 'order:' || o.id and pn.event_type = 'paid'\n"
        f"left join {SCHEMA}.dim_status ds on ds.code = o.status\n"
        "left join order_ratings r on r.order_id = o.id\n"
        "left join order_feedback f on f.order_id = o.id\n"
        "left join lateral (select s.id, s.source from dialog_sessions s where s.peer_id = o.peer_id\n"
        "  and s.started_at <= o.created_at order by s.started_at desc, s.id desc limit 1) sess on true\n"
        f"where {_REAL_ORDER}"
    ),
)

# Фасовка — хвост названия: «Те Гуань Инь 100 г» → «100 г».
_PACK = r"\s*(\d+([.,]\d+)?\s*(г|гр|кг|мл|л|шт)\.?)$"

V_ORDER_ITEMS = View(
    name="v_order_items",
    comment="Позиции заказов, по строке на товар в заказе, без тестовых заказов. Сумма — цена × количество.",
    columns=(
        Column("order_id", "o.id", "Номер заказа, связь с v_orders.order_id."),
        Column("client_key", "c.client_key", "Псевдонимный ключ клиента."),
        Column("line_no", "it.n::int", "Номер строки в заказе."),
        Column("item_name", "it.item->>'name'", "Товар как в заказе: название вместе с фасовкой."),
        Column("product", f"regexp_replace(it.item->>'name', {_quote(_PACK)}, '', 'i')", "Сорт без фасовки."),
        Column("pack", f"substring(it.item->>'name' from {_quote('(?i)' + _PACK[3:])})",
               "Фасовка из названия: «100 г», «50 г». Пусто — в названии фасовки нет."),
        Column("quantity", "(it.item->>'quantity')::int", "Количество пачек."),
        Column("price", "(it.item->>'price')::numeric(10,2)", "Цена за пачку, руб."),
        Column("amount", "((it.item->>'quantity')::numeric * (it.item->>'price')::numeric)::numeric(10,2)",
               "Сумма по строке, руб.: цена × количество."),
        Column("created_at", "o.created_at", "Когда создан заказ, UTC."),
        Column("created_date_msk", msk_date("o.created_at"), "Дата создания заказа по Москве."),
        Column("is_paid", f"coalesce({_PAID}, false)", "Заказ оплачен."),
        Column("status", "o.status", "Статус заказа кодом (dim_status)."),
    ),
    body=(
        "from orders o\n"
        "left join clients c on c.peer_id = o.peer_id\n"
        "cross join lateral jsonb_array_elements(o.items) with ordinality as it(item, n)\n"
        f"where {_REAL_ORDER}"
    ),
)

# Вид события: у кнопок и касаний — общий код, конкретика — в своих столбцах.
_KIND = (
    "case when e.event like 'button:%' then 'button' when e.event like 'touch:%' then 'touch' else e.event end"
)

V_FUNNEL_EVENTS = View(
    name="v_funnel_events",
    comment=(
        "События воронки по одному на строку, без тестовых клиентов и заказов. Подписи и порядок шагов — "
        "dim_event: сначала по точному коду (event), потом по общему (event_kind)."
    ),
    columns=(
        Column("event_id", "e.id", "Номер события."),
        Column("created_at", "e.created_at", "Когда случилось, UTC."),
        Column("created_date_msk", msk_date("e.created_at"), "Дата события по Москве."),
        Column("event", "e.event", "Код события, как пишет бот: draft_created, button:take, touch:repeat_nudge…"),
        Column("event_kind", _KIND, "Общий код: button — любое нажатие, touch — любое касание, иначе как event."),
        Column("event_label", "coalesce(de.label, dk.label, e.event)", "Подпись события по-русски."),
        Column("funnel_order", "coalesce(de.funnel_order, dk.funnel_order)",
               "Порядок шага в основной воронке; пусто — событие вне воронки."),
        Column("stage", "coalesce(de.stage, dk.stage)", "Этап воронки по-русски."),
        Column("client_key", "c.client_key", "Псевдонимный ключ клиента."),
        Column("order_id", "e.order_id", "Номер заказа, если событие про заказ. У событий черновика пусто."),
        Column("source", "e.source", "Источник кодом: text, button, code, storefront, reminder, manager, carrier, yookassa."),
        Column("source_label", _case("e.source", SOURCES), "Источник по-русски. Пусто — событие записано до 10.2026."),
        Column("button_action", "case when e.event like 'button:%' then substr(e.event, 8) else e.data->>'action' end",
               "Какая кнопка нажата: take, repeat, pt (пункт), rate, offer_ok…"),
        Column("touch", "coalesce(case when e.event like 'touch:%' then substr(e.event, 7) end, e.data->>'touch')",
               "Повторное касание, к которому относится событие: feedback_ask, repeat_nudge, second_touch, reactivation."),
        Column("origin", "e.data->>'origin'", "Откуда черновик (draft_created): text, take, repeat, storefront, returning, button."),
        Column("attempt", "(e.data->>'attempt')::int", "Номер попытки оплаты у событий счёта и оплаты."),
        Column("amount", "coalesce(e.data->>'amount', e.data->>'total', e.data->>'cost')::numeric(10,2)",
               "Сумма события, руб.: счёт, оплата или цена доставки."),
        Column("income_amount", "(e.data->>'income')::numeric(10,2)", "Сколько получит магазин после комиссии (payment_succeeded), руб."),
        Column("payment_method", "e.data->>'method'",
               "Способ оплаты (payment_succeeded) или способ доставки (delivery_quoted, point_chosen)."),
        Column("reason", "e.data->>'reason'", "Причина отказа банка от ЮKassa (payment_declined): insufficient_funds и т. п."),
        Column("rating", "e.data->>'value'", "Оценка (rated): great, ok, no."),
        Column("carrier", "e.data->>'carrier'", "Перевозчик (shipment_created): cdek, ozon."),
        Column("item", "e.data->>'item'", "Товар допродажи (upsell_offered, upsell_accepted)."),
        Column("dialog_id", "e.dialog_id", "Сессия диалога (v_dialogs.dialog_id). Пусто — событие до 10.2026."),
        Column("session_source", f"case when s.id is not null then {_session_source()} end",
               "Источник сессии: ad:<метка>, referral:<код>, organic, own."),
        Column("session_source_kind", f"case when s.id is not null then {_kind(_session_source())} end",
               "Вид источника сессии: ad, referral, organic, own."),
    ),
    body=(
        "from funnel_events e\n"
        "left join clients c on c.peer_id = e.peer_id\n"
        "left join orders o on o.id = e.order_id\n"
        "left join dialog_sessions s on s.id = e.dialog_id\n"
        f"left join {SCHEMA}.dim_event de on de.code = e.event\n"
        f"left join {SCHEMA}.dim_event dk on dk.code = {_KIND}\n"
        "where not coalesce(c.is_test, false) and not coalesce(o.is_test, false)"
    ),
)

V_TOUCHES = View(
    name="v_touches",
    comment=(
        "Повторные касания после покупки, по строке на отправленное касание, без тестовых клиентов. "
        "Результат касания засчитывается до следующего касания этому клиенту: нажатие и заказ — в течение "
        "7 дней, отписка — 2 дней."
    ),
    prefix=(
        "with touches as (\n"
        "  select e.id, e.peer_id, e.order_id, e.created_at, substr(e.event, 7) as kind,\n"
        "         least(e.created_at + interval '7 days',\n"
        "               coalesce(lead(e.created_at) over (partition by e.peer_id order by e.created_at),\n"
        "                        'infinity'::timestamptz)) as until\n"
        "  from funnel_events e where e.event like 'touch:%'\n"
        ")\n"
    ),
    columns=(
        Column("touch_id", "t.id", "Номер касания."),
        Column("client_key", "c.client_key", "Псевдонимный ключ клиента."),
        Column("touch", "t.kind", "Касание кодом: feedback_ask, repeat_nudge, second_touch, reactivation."),
        Column("touch_label", _case("t.kind", TOUCHES), "Касание по-русски."),
        Column("sent_at", "t.created_at", "Когда отправлено, UTC."),
        Column("sent_date_msk", msk_date("t.created_at"), "Дата отправки по Москве."),
        Column("order_id", "t.order_id", "Заказ, по поводу которого касание (у реактивации — последний заказ)."),
        Column(
            "pressed",
            "exists (select 1 from funnel_events b where b.peer_id = t.peer_id and b.event like 'button:%'"
            " and b.data->>'touch' = t.kind and b.created_at >= t.created_at and b.created_at < t.until)",
            "Клиент нажал кнопку под касанием.",
        ),
        Column(
            "pressed_action",
            "(select substr(b.event, 8) from funnel_events b where b.peer_id = t.peer_id and b.event like 'button:%'"
            " and b.data->>'touch' = t.kind and b.created_at >= t.created_at and b.created_at < t.until"
            " order by b.created_at limit 1)",
            "Какую кнопку нажал первой: repeat, rate, take, other, advise…",
        ),
        Column(
            "ordered_7d",
            "exists (select 1 from funnel_events x where x.peer_id = t.peer_id and x.event = 'touch_order'"
            " and x.data->>'touch' = t.kind and x.created_at >= t.created_at and x.created_at < t.until)",
            "Клиент оплатил заказ в течение 7 дней после касания.",
        ),
        Column(
            "new_order_id",
            "(select x.order_id from funnel_events x where x.peer_id = t.peer_id and x.event = 'touch_order'"
            " and x.data->>'touch' = t.kind and x.created_at >= t.created_at and x.created_at < t.until"
            " order by x.created_at limit 1)",
            "Номер заказа, оплаченного после касания.",
        ),
        Column(
            "opted_out_2d",
            "exists (select 1 from funnel_events x where x.peer_id = t.peer_id and x.event = 'touch_optout'"
            " and x.data->>'touch' = t.kind and x.created_at >= t.created_at"
            " and x.created_at < least(t.until, t.created_at + interval '2 days'))",
            "Клиент отписался («стоп») в течение 2 дней после касания.",
        ),
    ),
    body=(
        "from touches t\n"
        "left join clients c on c.peer_id = t.peer_id\n"
        "where not coalesce(c.is_test, false)"
    ),
)

V_CLIENTS = View(
    name="v_clients",
    comment=(
        "Клиенты, по строке на человека, без тестовых. Выручка — сумма total оплаченных заказов без "
        "возвращённых. Клиент появляется при первом сообщении или первом заказе."
    ),
    prefix=(
        "with o as (\n"
        "  select o.peer_id,\n"
        "         min(o.created_at) as first_order_at,\n"
        "         max(o.created_at) as last_order_at,\n"
        "         min(o.created_at) filter (where o.payment_status = 'succeeded') as first_paid_at,\n"
        "         max(o.created_at) filter (where o.payment_status = 'succeeded') as last_paid_at,\n"
        "         count(*) as orders,\n"
        "         count(*) filter (where o.payment_status = 'succeeded') as paid_orders,\n"
        "         sum(o.total) filter (where o.payment_status = 'succeeded' and o.status <> 'refunded') as revenue\n"
        "  from orders o where not o.is_test group by o.peer_id\n"
        ")\n"
    ),
    columns=(
        Column("client_key", "c.client_key", "Псевдонимный ключ клиента (HMAC от VK ID)."),
        Column("first_contact_at", "c.first_contact_at", "Первый контакт: первое сообщение или первый заказ, UTC."),
        Column("first_contact_date_msk", msk_date("c.first_contact_at"), "Дата первого контакта по Москве."),
        Column("first_order_at", "o.first_order_at", "Когда создан первый заказ (выставлен счёт), UTC."),
        Column("first_paid_order_at", "o.first_paid_at", "Когда создан первый оплаченный заказ, UTC."),
        Column("last_order_at", "o.last_order_at", "Когда создан последний заказ, UTC."),
        Column("last_paid_order_at", "o.last_paid_at", "Когда создан последний оплаченный заказ, UTC."),
        Column("orders_count", "coalesce(o.orders, 0)::int", "Сколько заказов создано (с выставленным счётом)."),
        Column("paid_orders_count", "coalesce(o.paid_orders, 0)::int", "Сколько заказов оплачено."),
        Column("revenue", "coalesce(o.revenue, 0)::numeric(10,2)", "Выручка с клиента, руб.: оплаченные заказы без возвращённых."),
        Column("opted_out", "coalesce(p.marketing_opt_out, false)", "Отписан от продающих сообщений («стоп»)."),
        Column("opted_out_at", "p.opted_out_at", "Когда отписался, UTC."),
        Column("unreachable", "p.unreachable_at is not null", "ВК не доставляет клиенту сообщения (запретил или удалил страницу)."),
        Column("ref", "c.ref", "Метка рекламной кампании при первом контакте. Пусто — органика."),
        Column("ref_source", "c.ref_source", "Источник метки кампании. Пусто — органика."),
        Column("first_source", _first_source(),
               "Первый источник (first touch): ad:<метка>, referral:<код>, organic, own («свои», метка /own)."),
        Column("first_source_kind", _kind(_first_source()), "Вид первого источника: ad, referral, organic, own."),
        Column("is_own", "c.own_at is not null", "«Свои» — помечен командой /own: в CAC не входит."),
    ),
    body=(
        "from clients c\n"
        "left join o on o.peer_id = c.peer_id\n"
        "left join client_preferences p on p.peer_id = c.peer_id\n"
        "where not c.is_test"
    ),
)

# --- справочники из таблицы: себестоимость и маржа ------------------------------
# Источник — вкладки «Товары», «Себестоимость», «Настройки», «Приходы» той же
# Google Таблицы, что каталог бота (`analytics/references.py` кладёт их в
# public.ref_*). Себестоимость и ставки берутся на дату оплаты заказа: строка
# с самой поздней датой «Действует с», не позже этой даты.

_TODAY_MSK = "(now() at time zone 'Europe/Moscow')::date"


def _cost_on(code: str, on_date: str) -> str:
    return (f"(select c.unit_cost from ref_costs c where c.code = {code} and c.valid_from <= {on_date} "
            "order by c.valid_from desc limit 1)")


V_REF_PRODUCTS = View(
    name="v_ref_products",
    comment=(
        "Товары из справочника (вкладка «Товары»), по строке на товар: розничная цена, себестоимость, "
        "действующая сегодня, маржа на единицу, и то же по цене, по которой продаёт бот (каталог бота "
        "связан по «Названию в каталоге бота»). Без продаж — это юнит-экономика справочника."
    ),
    columns=(
        Column("code", "p.code::text", "Код товара — общий для всех вкладок справочников."),
        Column("name", "p.name::text", "Название товара в справочнике."),
        Column("kind", "p.kind::text", "Тип: пачка, блин, набор, сет."),
        Column("weight_g", "p.weight_g::numeric(12,3)", "Вес, г."),
        Column("retail_price", "p.retail_price::numeric(12,2)", "Розничная цена из справочника, руб."),
        Column("unit_cost", "cost.unit_cost::numeric(12,2)",
               "Себестоимость единицы, действующая сегодня, руб. Пусто — себестоимость не задана."),
        Column("cost_valid_from", "cost.valid_from", "С какой даты действует эта себестоимость."),
        Column("unit_margin", "(p.retail_price - cost.unit_cost)::numeric(12,2)",
               "Маржа на единицу по розничной цене справочника, руб.: цена − себестоимость."),
        Column("margin_pct", "round(100 * (p.retail_price - cost.unit_cost) / nullif(p.retail_price, 0), 1)::numeric(8,1)",
               "Маржа по розничной цене справочника, % от цены."),
        Column("active", "p.active", "Товар активен в справочнике."),
        Column("role", "p.role::text", "Роль товара в ассортименте (первый заказ, допродажа…)."),
        Column("bot_name", "p.bot_name::text", "Название в каталоге бота. Пусто — «нет в каталоге»."),
        Column("in_bot_catalog", "bot.price is not null", "Бот сейчас продаёт этот товар (есть в «Каталоге SKU»)."),
        Column("bot_price", "bot.price::numeric(12,2)", "Цена, по которой продаёт бот, руб."),
        Column("bot_unit_margin", "(bot.price - cost.unit_cost)::numeric(12,2)",
               "Маржа на единицу по цене бота, руб."),
        Column("bot_margin_pct", "round(100 * (bot.price - cost.unit_cost) / nullif(bot.price, 0), 1)::numeric(8,1)",
               "Маржа по цене бота, % от цены."),
        Column("bot_in_stock", "bot.in_stock", "В каталоге бота отмечен «в наличии»."),
        Column("components", "comp.list", "Состав набора или сета: сорта и количество (вкладка «Состав»)."),
        Column("supplier_sku", "p.supplier_sku::text", "Артикул поставщика."),
    ),
    body=(
        "from ref_products p\n"
        "left join lateral (select c.unit_cost, c.valid_from from ref_costs c\n"
        f"  where c.code = p.code and c.valid_from <= {_TODAY_MSK} order by c.valid_from desc limit 1) cost on true\n"
        "left join lateral (select (e->>'price')::numeric as price,\n"
        "    coalesce((e->>'in_stock')::boolean, true) as in_stock\n"
        "  from catalog_snapshot s cross join lateral jsonb_array_elements(s.items::jsonb) e\n"
        "  where p.bot_name is not null and lower(e->>'name') = lower(p.bot_name) limit 1) bot on true\n"
        "left join lateral (select string_agg(coalesce(cp.name, x.component_code) || ' ' || x.quantity::float8 || "
        "coalesce(' ' || x.unit, ''), ' · ' order by x.component_code) as list\n"
        "  from ref_composition x left join ref_products cp on cp.code = x.component_code\n"
        "  where x.product_code = p.code and x.component_code <> p.code) comp on true"
    ),
)

V_REF_COSTS = View(
    name="v_ref_costs",
    comment="История себестоимости (вкладка «Себестоимость»): по строке на товар и дату «Действует с».",
    columns=(
        Column("code", "c.code::text", "Код товара."),
        Column("name", "p.name::text", "Название товара."),
        Column("valid_from", "c.valid_from", "Действует с этой даты."),
        Column("valid_to", "(lead(c.valid_from) over (partition by c.code order by c.valid_from) - 1)",
               "Действовала по эту дату включительно. Пусто — действует до сих пор."),
        Column("unit_cost", "c.unit_cost::numeric(12,2)", "Себестоимость единицы, руб."),
        Column("is_current", f"c.valid_from = max(c.valid_from) filter (where c.valid_from <= {_TODAY_MSK}) "
               "over (partition by c.code)", "Это значение действует сегодня."),
        Column("source", "c.source::text", "Откуда цифра (финмодель, счёт поставщика…)."),
        Column("comment", "c.comment::text", "Из чего сложилась себестоимость."),
    ),
    body="from ref_costs c\nleft join ref_products p on p.code = c.code",
)

V_REF_SETTINGS = View(
    name="v_ref_settings",
    comment=(
        "Параметры магазина, действующие сегодня (вкладка «Настройки»): эквайринг, чеки, УСН, упаковка, "
        "CAC… Проценты — долей (0.034 = 3,4 %)."
    ),
    columns=(
        Column("key", "s.key::text", "Ключ параметра."),
        Column("name", "s.name::text", "Что это за параметр."),
        Column("value", "s.value::numeric(16,6)", "Значение; для долей — 0.034 = 3,4 %."),
        Column("unit", "s.unit::text", "Единица: ₽/заказ, ₽/шт, доля…"),
        Column("valid_from", "s.valid_from", "Действует с этой даты."),
        Column("source", "s.source::text", "Откуда значение."),
        Column("comment", "s.comment::text", "Комментарий."),
    ),
    body=(
        "from (select distinct on (key) * from ref_settings\n"
        f"  where valid_from <= {_TODAY_MSK} order by key, valid_from desc) s"
    ),
)

V_REF_STOCK_MOVES = View(
    name="v_ref_stock_moves",
    comment="Приходы, списания и инвентаризация сортов (вкладка «Приходы и инвентаризация»).",
    columns=(
        Column("move_date", "m.move_date", "Дата операции."),
        Column("operation", "m.operation::text", "Операция: приход, списание, инвентаризация."),
        Column("code", "m.code::text", "Код сорта."),
        Column("name", "p.name::text", "Название сорта."),
        Column("quantity", "m.quantity::numeric(14,3)", "Количество."),
        Column("unit", "m.unit::text", "Единица: г, шт."),
        Column("amount", "m.amount::numeric(12,2)", "Сумма партии, руб."),
        Column("unit_amount", "round(m.amount / nullif(m.quantity, 0), 4)::numeric(14,4)", "Сумма за единицу (за грамм или штуку), руб."),
        Column("document", "nullif(m.document, '')::text", "Документ: заявка, накладная."),
        Column("comment", "m.comment::text", "Комментарий."),
    ),
    body="from ref_stock_moves m\nleft join ref_products p on p.code = m.code",
)

_ON_DATE = "coalesce(o.paid_date_msk, o.created_date_msk)"

V_ORDER_ITEM_MARGIN = View(
    name="v_order_item_margin",
    comment=(
        "Позиции заказов с себестоимостью на дату оплаты, без тестовых заказов. Товар связан со справочником "
        "по «Названию в каталоге бота». Маржа строки — сумма минус себестоимость, без эквайринга, налога и "
        "доставки (они — в v_order_margin)."
    ),
    columns=(
        Column("order_id", "li.order_id", "Номер заказа."),
        Column("line_no", "li.line_no", "Номер строки в заказе."),
        Column("paid_date_msk", "o.paid_date_msk", "Дата оплаты по Москве."),
        Column("is_paid", "o.is_paid", "Заказ оплачен."),
        Column("is_refunded", "o.is_refunded", "По заказу был возврат."),
        Column("item_name", "li.item_name", "Товар как в заказе."),
        Column("product_code", "ref.code::text", "Код товара в справочнике. Пусто — не связан со справочником."),
        Column("product_name", "ref.name::text", "Название в справочнике."),
        Column("quantity", "li.quantity", "Количество."),
        Column("amount", "li.amount", "Сумма по строке, руб."),
        Column("unit_cost", f"{_cost_on('ref.code', _ON_DATE)}::numeric(12,2)",
               "Себестоимость единицы на дату оплаты, руб. Пусто — себестоимость не задана."),
        Column("cogs", f"(li.quantity * {_cost_on('ref.code', _ON_DATE)})::numeric(12,2)",
               "Себестоимость строки, руб.: количество × себестоимость единицы."),
        Column("gross_margin", f"(li.amount - li.quantity * {_cost_on('ref.code', _ON_DATE)})::numeric(12,2)",
               "Валовая маржа строки, руб.: сумма − себестоимость."),
        Column("has_cost", f"{_cost_on('ref.code', _ON_DATE)} is not null",
               "Себестоимость найдена. Нет — товар не связан со справочником или себестоимость не задана."),
    ),
    body=(
        f"from {SCHEMA}.v_order_items li\n"
        f"join {SCHEMA}.v_orders o on o.order_id = li.order_id\n"
        "left join lateral (select p.code, p.name from ref_products p\n"
        "  where p.bot_name is not null and lower(p.bot_name) in (lower(li.item_name), lower(li.product))\n"
        "  order by p.code limit 1) ref on true"
    ),
)


def _setting(key: str) -> str:
    return f"max(cur.value) filter (where cur.key = {_quote(key)})"


V_ORDER_MARGIN = View(
    name="v_order_margin",
    comment=(
        "Маржа по заказу, по строке на заказ, без тестовых. Выручка — total (товары + доставка для клиента); "
        "из неё вычитаются себестоимость товаров, эквайринг с НДС на комиссию, чеки, резерв УСН, цена "
        "перевозчика, прочие переменные расходы и Claude API — по ставкам «Настроек» на дату оплаты. "
        "Считать по оплаченным без возврата (is_paid and not is_refunded)."
    ),
    columns=(
        Column("order_id", "o.order_id", "Номер заказа."),
        Column("created_date_msk", "o.created_date_msk", "Дата создания по Москве."),
        Column("paid_date_msk", "o.paid_date_msk", "Дата оплаты по Москве."),
        Column("is_paid", "o.is_paid", "Заказ оплачен."),
        Column("is_refunded", "o.is_refunded", "По заказу был возврат."),
        Column("total", "o.total", "Выручка заказа, руб.: товары + доставка для клиента."),
        Column("items_total", "o.items_total", "Товары, руб."),
        Column("cogs", "coalesce(li.cogs, 0)::numeric(12,2)", "Себестоимость товаров, руб."),
        Column("lines_without_cost", "coalesce(li.missing, 0)::int",
               "Строк без себестоимости: маржа заказа завышена на их себестоимость."),
        Column("acquiring", f"round(o.total * coalesce({_setting('acquiring_cards_rate')}, 0) "
               f"* (1 + coalesce({_setting('acquiring_vat_rate')}, 0)), 2)::numeric(12,2)",
               "Эквайринг с НДС на комиссию, руб."),
        Column("fiscalization", f"round(o.total * coalesce({_setting('fiscalization_rate')}, 0), 2)::numeric(12,2)",
               "Чеки (фискализация), руб."),
        Column("usn_reserve", f"round(o.total * coalesce({_setting('usn_reserve_rate')}, 0), 2)::numeric(12,2)",
               "Резерв УСН, руб."),
        Column("delivery_expense", "o.carrier_delivery_cost",
               "Цена перевозчика, руб. (с надбавкой на эквайринг, которую бот кладёт в цену доставки)."),
        Column("other_variable", f"coalesce({_setting('order_other_variable_rub')}, 0)::numeric(12,2)",
               "Прочие переменные расходы на заказ (коробка, наполнитель…), руб."),
        Column("claude_api", f"round(coalesce({_setting('api_usd_per_consultation')}, 0) "
               f"* coalesce({_setting('usd_rub')}, 0), 2)::numeric(12,2)", "Claude API на консультацию, руб. (оценка)."),
        Column(
            "contribution_margin",
            f"(o.total - coalesce(li.cogs, 0)"
            f" - round(o.total * coalesce({_setting('acquiring_cards_rate')}, 0) * (1 + coalesce({_setting('acquiring_vat_rate')}, 0)), 2)"
            f" - round(o.total * coalesce({_setting('fiscalization_rate')}, 0), 2)"
            f" - round(o.total * coalesce({_setting('usn_reserve_rate')}, 0), 2)"
            f" - o.carrier_delivery_cost - coalesce({_setting('order_other_variable_rub')}, 0)"
            f" - round(coalesce({_setting('api_usd_per_consultation')}, 0) * coalesce({_setting('usd_rub')}, 0), 2))::numeric(12,2)",
            "Маржинальная прибыль заказа, руб.: выручка минус все переменные расходы выше.",
        ),
        Column(
            "margin_pct",
            f"round(100 * (o.total - coalesce(li.cogs, 0)"
            f" - round(o.total * coalesce({_setting('acquiring_cards_rate')}, 0) * (1 + coalesce({_setting('acquiring_vat_rate')}, 0)), 2)"
            f" - round(o.total * coalesce({_setting('fiscalization_rate')}, 0), 2)"
            f" - round(o.total * coalesce({_setting('usn_reserve_rate')}, 0), 2)"
            f" - o.carrier_delivery_cost - coalesce({_setting('order_other_variable_rub')}, 0)"
            f" - round(coalesce({_setting('api_usd_per_consultation')}, 0) * coalesce({_setting('usd_rub')}, 0), 2))"
            " / nullif(o.total, 0), 1)::numeric(8,1)",
            "Маржинальная прибыль, % выручки.",
        ),
    ),
    body=(
        f"from {SCHEMA}.v_orders o\n"
        "left join lateral (select sum(m.cogs) as cogs, count(*) filter (where not m.has_cost) as missing\n"
        f"  from {SCHEMA}.v_order_item_margin m where m.order_id = o.order_id) li on true\n"
        "left join lateral (select distinct on (s.key) s.key, s.value from ref_settings s\n"
        f"  where s.valid_from <= {_ON_DATE} order by s.key, s.valid_from desc) cur on true\n"
        "group by o.order_id, o.created_date_msk, o.paid_date_msk, o.is_paid, o.is_refunded, o.total, o.items_total,\n"
        "  o.carrier_delivery_cost, li.cogs, li.missing"
    ),
)

V_LLM_USAGE = View(
    name="v_llm_usage",
    comment=(
        "Расход Claude API по запросам: модель, токены и стоимость в долларах по ценам Anthropic "
        "(app/modules/ops/llm_prices.py). Строка на каждый ответ API, включая повтор запасной моделью. "
        "Тестовые диалоги тоже здесь: это реальный расход."
    ),
    columns=(
        Column("at", "u.at", "Когда пришёл ответ, UTC."),
        Column("date_msk", msk_date("u.at"), "Дата по Москве."),
        Column("operation", "u.operation::text", "Что делал бот: ход диалога, ассистент Ops, пересказ диалога…"),
        Column("model", "u.model::text", "Модель, которая ответила."),
        Column("input_tokens", "u.input_tokens", "Токены входа без кэша."),
        Column("output_tokens", "u.output_tokens", "Токены выхода, включая размышление."),
        Column("cache_read_tokens", "u.cache_read_tokens", "Токены, прочитанные из кэша промпта (0,1 цены входа)."),
        Column("cache_write_tokens", "u.cache_write_tokens", "Токены, записанные в кэш (1,25 цены входа)."),
        Column("cost_usd", "u.cost_usd::numeric(12,6)", "Стоимость запроса, $. Пусто — цены модели нет в справочнике цен."),
        Column("stop_reason", "u.stop_reason::text", "Чем закончился ответ: end_turn, tool_use, refusal…"),
        Column("client_key", "u.client_key", "Клиент, чей ход (псевдонимный ключ). Пусто — не ход клиента (ассистент Ops)."),
        Column("dialog_id", "u.dialog_id", "Сессия диалога (v_dialogs.dialog_id)."),
        Column("order_id", "u.order_id", "Открытый заказ на момент вызова: выставленный счёт или правка заказа."),
        Column("stage", "u.stage::text",
               "Этап на момент вызова: consultation, draft_created, delivery_quoted, point_chosen, recipient_set, invoice."),
        Column("purpose", "u.purpose::text", "Назначение: sales_bot — продавец, ops_assistant — чат Ops, labeling — пересказ диалога."),
        Column("cost_rub", f"(u.cost_usd * {SCHEMA}.usd_rub({msk_date('u.at')}))::numeric(12,2)",
               "Стоимость, руб. — по курсу вводной usd_rub на дату (fin, иначе вкладка «Настройки»)."),
        Column("is_test_client", "coalesce(c.is_test, false)", "Ход тестового аккаунта: в рубли на диалог не входит."),
    ),
    body="from llm_usage u\nleft join clients c on c.client_key = u.client_key",
)

V_ANTHROPIC_COSTS = View(
    name="v_anthropic_costs",
    comment=(
        "Счёт Anthropic по дням из отчёта о расходах (Admin API cost_report): то, что будет выставлено "
        "к оплате, по всей организации. Строка — день × статья (модель и вид токенов). Сравнивать с "
        "v_llm_usage — оценкой бота по ценам."
    ),
    columns=(
        Column("day", "c.day", "День (UTC — так считает Anthropic)."),
        Column("description", "c.description::text", "Статья счёта, как у Anthropic: «Claude Sonnet 5.5 Usage - Input Tokens»."),
        Column("model", "c.model::text", "Модель. Пусто — не токены (веб-поиск, код)."),
        Column("cost_type", "c.cost_type::text", "Вид расхода: tokens, web_search, code_execution…"),
        Column("token_type", "c.token_type::text", "Вид токенов: uncached_input_tokens, output_tokens, cache_read_input_tokens…"),
        Column("service_tier", "c.service_tier::text", "standard или batch."),
        Column("amount_usd", "c.amount_usd::numeric(14,6)", "Сумма, $."),
    ),
    body="from anthropic_costs c",
)

# --- сессии диалога, расход Claude на диалог, шаги воронки ------------------------

# Курс доллара на дату: вводная usd_rub финмодели (fin.inp), а пока схемы fin
# нет — вкладка «Настройки» таблицы (ref_settings). Динамический вызов: без
# fin функция всё равно создаётся. SECURITY DEFINER — Metabase (metabase_ro)
# читает только analytics, а курс лежит в fin и public.
FUNCTIONS = (
    f"""create or replace function {SCHEMA}.usd_rub(on_date date) returns numeric
    language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare v numeric;
begin
  if to_regprocedure('fin.inp(text,date)') is not null then
    execute 'select fin.inp($1, $2)' into v using 'usd_rub'::text, on_date;
    if v is not null then return v; end if;
  end if;
  select value into v from public.ref_settings where key = 'usd_rub' and valid_from <= on_date
   order by valid_from desc limit 1;
  return v;
end $$""",
    f"comment on function {SCHEMA}.usd_rub(date) is 'Курс доллара на дату: fin.inp(usd_rub), иначе ref_settings.usd_rub. NULL — нет данных.'",
)

# Счета — все способы выставить счёт (dim_event, этап «Счёт», порядок 60).
_INVOICE_EVENTS = ("invoice_auto", "invoice_confirmed", "invoice_returning", "invoice_repeat", "invoice_manual",
                   "invoice_single_point")
_INVOICE_LIST = ", ".join(_quote(e) for e in _INVOICE_EVENTS)

V_DIALOGS = View(
    name="v_dialogs",
    comment=(
        "Сессии диалога, по строке на сессию, без тестовых клиентов. Сессия — от сообщения клиента до суток "
        "тишины или оплаты. Источник — откуда пришло первое сообщение сессии (last touch); консультация, "
        "оплата и расход Claude — в этой сессии."
    ),
    prefix=(
        "with cost as (\n"
        "  select dialog_id, count(*) as requests, sum(cost_usd) as usd,\n"
        f"         sum(cost_usd * {SCHEMA}.usd_rub({msk_date('at')})) as rub\n"
        "  from llm_usage where dialog_id is not null group by dialog_id\n"
        "), paid as (\n"
        "  select dialog_id, min(order_id) as order_id from funnel_events\n"
        "  where event = 'payment_succeeded' and dialog_id is not null group by dialog_id\n"
        ")\n"
    ),
    columns=(
        Column("dialog_id", "s.id", "Номер сессии диалога."),
        Column("client_key", "c.client_key", "Псевдонимный ключ клиента."),
        Column("started_at", "s.started_at", "Первое сообщение сессии, UTC."),
        Column("started_date_msk", msk_date("s.started_at"), "Дата начала сессии по Москве."),
        Column("last_message_at", "s.last_message_at", "Последнее сообщение клиента в сессии, UTC."),
        Column("closed_by_payment", "s.close_reason = 'payment'", "Сессию закрыла оплата заказа."),
        Column("client_dialog_number", "row_number() over (partition by s.peer_id order by s.started_at, s.id)::int",
               "Порядковый номер сессии у клиента: 1 — первый диалог."),
        Column("session_source", _session_source(), "Источник сессии: ad:<метка>, referral:<код>, organic, own."),
        Column("session_source_kind", _kind(_session_source()), "Вид источника: ad, referral, organic, own."),
        Column("ref", "s.ref::text", "Метка из ссылки (ref), с которой пришло первое сообщение сессии."),
        Column("first_source", _first_source(), "Первый источник клиента (first touch)."),
        Column("consult_started", "s.consult_started_at is not null", "В сессии была консультация (consult_started)."),
        Column("consult_qualified", "s.consult_qualified_at is not null",
               "Бот назвал товар из каталога (consult_qualified)."),
        Column("paid_order_id", "p.order_id", "Заказ, оплаченный в этой сессии. Пусто — без оплаты."),
        Column("is_paid", "p.order_id is not null", "В сессии оплачен заказ."),
        Column("is_first_purchase", "coalesce(vo.client_paid_order_number = 1, false)",
               "Оплачен первый заказ клиента — новый покупатель."),
        Column("llm_requests", "coalesce(k.requests, 0)::int", "Запросов к Claude в сессии."),
        Column("llm_cost_usd", "coalesce(k.usd, 0)::numeric(12,4)", "Расход Claude на сессию, $."),
        Column("llm_cost_rub", "k.rub::numeric(12,2)", "Расход Claude на сессию, руб. Пусто — курса нет или запросов не было."),
    ),
    body=(
        "from dialog_sessions s\n"
        "join clients c on c.peer_id = s.peer_id\n"
        "left join cost k on k.dialog_id = s.id\n"
        "left join paid p on p.dialog_id = s.id\n"
        f"left join {SCHEMA}.v_orders vo on vo.order_id = p.order_id\n"
        "where not c.is_test"
    ),
)

V_LLM_COST_BY_DIALOG = View(
    name="v_llm_cost_by_dialog",
    comment=(
        "Расход Claude в рублях по месяцам начала диалога и источнику: на диалог, на оплаченный заказ и на "
        "нового покупателя. Весь расход месяца делится на оплаты: диалоги без покупки — цена тех, что купили. "
        "Без тестовых клиентов; курс — вводная usd_rub."
    ),
    columns=(
        Column("month", "date_trunc('month', d.started_date_msk)::date", "Месяц начала сессий."),
        Column("session_source_kind", "d.session_source_kind", "Вид источника: ad, referral, organic, own."),
        Column("dialogs", "count(*)::int", "Сессий диалога."),
        Column("paid_orders", "count(*) filter (where d.is_paid)::int", "Оплаченных в этих сессиях заказов."),
        Column("new_buyers", "count(*) filter (where d.is_first_purchase)::int", "Новых покупателей (первая оплата)."),
        Column("cost_usd", "sum(d.llm_cost_usd)::numeric(12,4)", "Расход Claude, $."),
        Column("cost_rub", "sum(d.llm_cost_rub)::numeric(12,2)", "Расход Claude, руб."),
        Column("rub_per_dialog", "(sum(d.llm_cost_rub) / nullif(count(*), 0))::numeric(12,2)", "Рублей на диалог."),
        Column("rub_per_paid_order",
               "(sum(d.llm_cost_rub) / nullif(count(*) filter (where d.is_paid), 0))::numeric(12,2)",
               "Рублей на оплаченный заказ. Пусто — оплат не было."),
        Column("rub_per_new_buyer",
               "(sum(d.llm_cost_rub) / nullif(count(*) filter (where d.is_first_purchase), 0))::numeric(12,2)",
               "Рублей на нового покупателя. Пусто — новых покупателей не было."),
    ),
    body=f"from {SCHEMA}.v_dialogs d\ngroup by 1, 2",
)

# Шаги воронки по сессиям: время каждого шага — первое его событие в сессии.
FUNNEL_STEPS = [
    (1, "consult_started", "Консультация", ("consult_started",)),
    (2, "draft_created", "Черновик", ("draft_created",)),
    (3, "delivery_quoted", "Доставка посчитана", ("delivery_quoted",)),
    (4, "point_chosen", "Пункт выбран", ("point_chosen",)),
    (5, "recipient_set", "Получатель", ("recipient_set",)),
    (6, "invoice", "Счёт", _INVOICE_EVENTS),
    (7, "payment_succeeded", "Оплата", ("payment_succeeded",)),
]
_STEP_VALUES = ",\n    ".join(
    f"({n}, {_quote(code)}, {_quote(label)}, array[{', '.join(_quote(e) for e in events)}])"
    for n, code, label, events in FUNNEL_STEPS
)

V_FUNNEL_DIALOG_STEPS = View(
    name="v_funnel_dialog_steps",
    comment=(
        "Шаги воронки по сессиям диалога: строка на сессию × шаг (консультация → черновик → доставка → пункт → "
        "получатель → счёт → оплата). Дошёл — есть время шага; минут до следующего — до ближайшего дальнейшего "
        "пройденного шага (у курьера шага «пункт» нет). Без тестовых клиентов."
    ),
    prefix=(
        "with steps(step_no, step, step_label, events) as (values\n"
        f"    {_STEP_VALUES}\n"
        "), t as (\n"
        "  select d.dialog_id, d.client_key, d.started_date_msk, d.session_source, d.session_source_kind,\n"
        "         st.step_no, st.step, st.step_label, min(e.created_at) as at\n"
        f"  from {SCHEMA}.v_dialogs d cross join steps st\n"
        "  left join funnel_events e on e.dialog_id = d.dialog_id and e.event = any(st.events)\n"
        "  group by 1, 2, 3, 4, 5, 6, 7, 8\n"
        ")\n"
    ),
    columns=(
        Column("dialog_id", "t.dialog_id", "Сессия диалога."),
        Column("client_key", "t.client_key", "Псевдонимный ключ клиента."),
        Column("started_date_msk", "t.started_date_msk", "Дата начала сессии по Москве."),
        Column("session_source", "t.session_source", "Источник сессии."),
        Column("session_source_kind", "t.session_source_kind", "Вид источника: ad, referral, organic, own."),
        Column("step_no", "t.step_no::int", "Номер шага: 1 — консультация … 7 — оплата."),
        Column("step", "t.step::text", "Шаг кодом: consult_started, draft_created, delivery_quoted, point_chosen, "
               "recipient_set, invoice, payment_succeeded."),
        Column("step_label", "t.step_label::text", "Шаг по-русски."),
        Column("reached_at", "t.at", "Когда шаг пройден, UTC. Пусто — не дошёл."),
        Column("reached", "t.at is not null", "Шаг пройден."),
        Column("next_at", "nx.at", "Время ближайшего следующего пройденного шага, UTC."),
        Column("dropped", "t.at is not null and nx.at is null and t.step_no < 7",
               "Дошёл до шага и дальше не пошёл (отвал на этом шаге)."),
        Column("minutes_to_next", "round(extract(epoch from (nx.at - t.at)) / 60.0, 1)::numeric(12,1)",
               "Минут от этого шага до следующего пройденного."),
    ),
    body=(
        "from t\n"
        "left join lateral (select min(t2.at) as at from t t2 where t2.dialog_id = t.dialog_id\n"
        "  and t2.step_no > t.step_no and t2.at is not null) nx on true"
    ),
)

V_FUNNEL_STEPS = View(
    name="v_funnel_steps",
    comment=(
        "Воронка по шагам за всё время, по виду источника сессии и итогом (session_source_kind = 'all'): "
        "сколько сессий дошли до шага, сколько на нём отвалились, медиана и 90-й перцентиль минут до "
        "следующего шага. За период — считать по v_funnel_dialog_steps с фильтром по started_date_msk."
    ),
    columns=(
        Column("session_source_kind", "coalesce(f.session_source_kind, 'all')",
               "Вид источника: ad, referral, organic, own; all — все вместе."),
        Column("step_no", "f.step_no", "Номер шага."),
        Column("step", "f.step", "Шаг кодом."),
        Column("step_label", "f.step_label", "Шаг по-русски."),
        Column("reached", "count(*) filter (where f.reached)::int", "Сессий дошли до шага."),
        Column("dropped", "count(*) filter (where f.dropped)::int", "Отвалились на шаге."),
        Column("drop_pct", "round(100.0 * count(*) filter (where f.dropped) / nullif(count(*) filter (where f.reached), 0), 1)"
               "::numeric(6,1)", "Доля отвалившихся от дошедших, %."),
        Column("median_minutes_to_next",
               "(percentile_cont(0.5) within group (order by f.minutes_to_next))::numeric(12,1)",
               "Медиана минут до следующего шага."),
        Column("p90_minutes_to_next",
               "(percentile_cont(0.9) within group (order by f.minutes_to_next))::numeric(12,1)",
               "90-й перцентиль минут до следующего шага."),
    ),
    body=(
        f"from {SCHEMA}.v_funnel_dialog_steps f\n"
        "group by grouping sets ((f.session_source_kind, f.step_no, f.step, f.step_label), (f.step_no, f.step, f.step_label))"
    ),
)

V_INVOICE_TIMING = View(
    name="v_invoice_timing",
    comment=(
        "Заказы со счётом, по строке на заказ, без тестовых: время от черновика до первого счёта и истёк ли "
        "счёт. Черновик — последний draft_created клиента до первого счёта. Доля истёкших — avg(had_expired)."
    ),
    prefix=(
        "with inv as (\n"
        "  select e.order_id, e.peer_id, min(e.created_at) as first_invoice_at, count(*) as attempts,\n"
        "         (array_agg(e.dialog_id order by e.created_at))[1] as dialog_id\n"
        f"  from funnel_events e where e.event in ({_INVOICE_LIST}) and e.order_id is not null\n"
        "  group by e.order_id, e.peer_id\n"
        ")\n"
    ),
    columns=(
        Column("order_id", "inv.order_id", "Номер заказа."),
        Column("client_key", "c.client_key", "Псевдонимный ключ клиента."),
        Column("first_invoice_at", "inv.first_invoice_at", "Первый счёт по заказу, UTC."),
        Column("first_invoice_date_msk", msk_date("inv.first_invoice_at"), "Дата первого счёта по Москве."),
        Column("draft_created_at", "dr.at", "Черновик, из которого выставлен счёт, UTC. Пусто — счёт без черновика."),
        Column("minutes_draft_to_invoice", "round(extract(epoch from (inv.first_invoice_at - dr.at)) / 60.0, 1)::numeric(12,1)",
               "Минут от черновика до первого счёта."),
        Column("invoice_attempts", "inv.attempts::int", "Сколько раз выставлялся счёт."),
        Column("had_expired", "exists (select 1 from funnel_events x where x.order_id = inv.order_id and x.event = 'invoice_expired')",
               "Хоть один счёт по заказу истёк."),
        Column("is_paid", "coalesce(o.payment_status = 'succeeded', false)", "Заказ оплачен."),
        Column("session_source", f"case when s.id is not null then {_session_source()} end", "Источник сессии первого счёта."),
        Column("session_source_kind", f"case when s.id is not null then {_kind(_session_source())} end",
               "Вид источника: ad, referral, organic, own."),
    ),
    body=(
        "from inv\n"
        "join orders o on o.id = inv.order_id\n"
        "left join clients c on c.peer_id = inv.peer_id\n"
        "left join dialog_sessions s on s.id = inv.dialog_id\n"
        "left join lateral (select max(d.created_at) as at from funnel_events d where d.peer_id = inv.peer_id\n"
        "  and d.event = 'draft_created' and d.created_at <= inv.first_invoice_at) dr on true\n"
        "where not o.is_test and not coalesce(c.is_test, false)"
    ),
)

V_DELIVERY_OUTCOMES = View(
    name="v_delivery_outcomes",
    comment=(
        "Исход доставки по месяцам прибытия в пункт и перевозчику, без тестовых: сколько посылок в пункте, "
        "вручено, не выкуплено, доля невыкупа = не выкуплено / (вручено + не выкуплено), напоминания (C4)."
    ),
    columns=(
        Column("month", "date_trunc('month', coalesce(o.at_pickup_at, o.handed_over_at) at time zone 'Europe/Moscow')::date",
               "Месяц прибытия в пункт (или передачи перевозчику), по Москве."),
        Column("carrier", "case when o.ozon_posting is not null then 'ozon' else 'cdek' end", "Перевозчик: ozon, cdek."),
        Column("at_pickup", "count(*) filter (where o.at_pickup_at is not null)::int", "Посылок дошло до пункта выдачи."),
        Column("delivered", "count(*) filter (where o.delivered_at is not null)::int", "Вручено."),
        Column("not_delivered", "count(*) filter (where o.not_delivered_at is not null)::int", "Не выкуплено (вернулось)."),
        Column("non_pickup_share",
               "round(count(*) filter (where o.not_delivered_at is not null)::numeric / nullif(count(*) filter "
               "(where o.delivered_at is not null or o.not_delivered_at is not null), 0), 4)::numeric(6,4)",
               "Доля невыкупа: не выкуплено / (вручено + не выкуплено). Пусто — исходов ещё нет."),
        Column("reminded", "count(*) filter (where exists (select 1 from funnel_events e where e.order_id = o.id"
                           " and e.event in ('pickup_reminder_1', 'pickup_reminder_2')))::int",
               "Посылок, по которым ушло напоминание о пункте выдачи."),
    ),
    body=(
        "from orders o\n"
        "left join clients c on c.peer_id = o.peer_id\n"
        "where o.payment_status = 'succeeded' and (o.at_pickup_at is not null or o.handed_over_at is not null)\n"
        f"  and {_REAL_ORDER}\n"
        "group by 1, 2"
    ),
)

# Порядок важен: справочники раньше представлений, которые к ним джойнятся.
VIEWS = (DIM_STATUS, DIM_EVENT, V_ORDERS, V_ORDER_ITEMS, V_FUNNEL_EVENTS, V_TOUCHES, V_CLIENTS,
         V_REF_PRODUCTS, V_REF_COSTS, V_REF_SETTINGS, V_REF_STOCK_MOVES, V_ORDER_ITEM_MARGIN, V_ORDER_MARGIN,
         V_LLM_USAGE, V_ANTHROPIC_COSTS, V_DIALOGS, V_LLM_COST_BY_DIALOG, V_FUNNEL_DIALOG_STEPS, V_FUNNEL_STEPS,
         V_INVOICE_TIMING, V_DELIVERY_OUTCOMES)


def statements() -> list[str]:
    result = [f"create schema if not exists {SCHEMA}", *FUNCTIONS]
    for view in VIEWS:
        result.append(view.create_sql())
        result.extend(view.comment_sql())
    return result


async def create(conn) -> None:
    """Пересоздать схему analytics. Ошибка — строка в логе: бот без неё работает.

    Каждое представление — в своей точке сохранения: сломанное (например,
    кто-то убрал столбец в коде, и Postgres не дал заменить) остаётся
    прежним, остальные обновляются.
    """
    await conn.exec_driver_sql(f"select pg_advisory_xact_lock({_LOCK_KEY})")
    await conn.exec_driver_sql(f"create schema if not exists {SCHEMA}")
    try:
        async with conn.begin_nested():
            for statement in FUNCTIONS:
                await conn.exec_driver_sql(statement)
    except Exception:
        logger.exception("Аналитика: не пересоздали функции схемы %s", SCHEMA)
    for view in VIEWS:
        try:
            async with conn.begin_nested():
                await conn.exec_driver_sql(view.create_sql())
                for statement in view.comment_sql():
                    await conn.exec_driver_sql(statement)
        except Exception:
            logger.exception("Аналитика: не пересоздали представление %s.%s", SCHEMA, view.name)
