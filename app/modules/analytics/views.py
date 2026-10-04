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
    ),
    body=(
        "from funnel_events e\n"
        "left join clients c on c.peer_id = e.peer_id\n"
        "left join orders o on o.id = e.order_id\n"
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
    ),
    body=(
        "from clients c\n"
        "left join o on o.peer_id = c.peer_id\n"
        "left join client_preferences p on p.peer_id = c.peer_id\n"
        "where not c.is_test"
    ),
)

# Порядок важен: справочники раньше представлений, которые к ним джойнятся.
VIEWS = (DIM_STATUS, DIM_EVENT, V_ORDERS, V_ORDER_ITEMS, V_FUNNEL_EVENTS, V_TOUCHES, V_CLIENTS)


def statements() -> list[str]:
    result = [f"create schema if not exists {SCHEMA}"]
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
    for view in VIEWS:
        try:
            async with conn.begin_nested():
                await conn.exec_driver_sql(view.create_sql())
                for statement in view.comment_sql():
                    await conn.exec_driver_sql(statement)
        except Exception:
            logger.exception("Аналитика: не пересоздали представление %s.%s", SCHEMA, view.name)
