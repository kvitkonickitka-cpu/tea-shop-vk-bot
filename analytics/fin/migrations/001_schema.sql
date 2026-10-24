-- 001: схема fin — финмодель «Двух чайников» в базе бота.
--
-- Сюда переехала Google-таблица «Финмодель v1.5»: вводные и ручные события
-- (расходы мимо счёта, вклады партнёров, партии, фасовка, реклама) вносят
-- Никита и Илья формами Metabase, а факт берётся из базы бота (схема
-- analytics) и из выписки банка (схема finance, её ведёт cashflow).
--
-- Правила, которые держит схема:
-- - таблицы бота и finance здесь не меняются — только читаются представлениями;
-- - вводные не перезаписываются: каждое изменение — новая строка fin.inputs,
--   действующее значение на дату берёт fin.inp();
-- - кто внёс — обязательное поле entered_by: «Никита» или «Илья»; строки,
--   залитые из v1.5, помечены «импорт v1.5»;
-- - персональных данных клиентов в fin нет и быть не должно.
--
-- Применяется analytics/fin/apply.sh, миграции не переписываются — новое
-- новым файлом, как в cashflow.

create schema if not exists fin;

create table if not exists fin.schema_migrations (
    version    text primary key,
    applied_at timestamptz not null default now()
);

-- Кто вносит данные. Форма Metabase даёт выбрать только двоих; импорт —
-- для строк, перенесённых из таблицы v1.5.
create or replace function fin.valid_author(name text) returns boolean
    language sql immutable as $$ select name in ('Никита', 'Илья', 'импорт v1.5') $$;

-- Вводные с историей. Значение числом (value) или текстом/датой (value_text).
create table if not exists fin.inputs (
    id          bigserial primary key,
    key         text        not null,
    label       text,
    value       numeric,
    value_text  text,
    unit        text,
    status      text,
    note        text,
    valid_from  date        not null default current_date,
    entered_by  text        not null check (fin.valid_author(entered_by)),
    created_at  timestamptz not null default now(),
    -- Пустую вводную можно записать только явно, статусом «Нет данных»: форма
    -- без значения иначе молча превратила бы ставку в «нет данных».
    constraint inputs_value_present check (value is not null or value_text is not null or status = 'Нет данных')
);
create index if not exists inputs_key_idx on fin.inputs (key, valid_from desc, created_at desc);
comment on table fin.inputs is
    'Вводные финмодели с историей: каждое изменение — новая строка. Действующее значение — fin.inputs_current и fin.inp().';

create table if not exists fin.expenses (
    id          bigserial primary key,
    date        date          not null,
    category    text          not null check (category in (
                    'закупка', 'логистика', 'упаковка', 'оборудование', 'реклама', 'подрядчик',
                    'сервисы', 'налоги_взносы', 'банк', 'маркировка', 'прочее')),
    amount      numeric(14,2) not null check (amount > 0),
    description text,
    is_plan     boolean       not null default false,
    -- Деньги считаются по выписке банка. Расход, оплаченный со счёта ИП, уже
    -- есть в выписке — здесь он для описания и бюджета и в P&L и деньги не
    -- идёт второй раз. В счёт идут только оплаченные «мимо счёта».
    paid_from   text          not null default 'счёт ИП' check (paid_from in ('счёт ИП', 'мимо счёта')),
    entered_by  text          not null check (fin.valid_author(entered_by)),
    created_at  timestamptz   not null default now()
);
comment on table fin.expenses is
    'Расходы, внесённые руками: план (is_plan) и оплаченные мимо счёта. Оплаченные со счёта ИП берутся из выписки (finance), здесь — описание.';

create table if not exists fin.capital (
    id          bigserial primary key,
    date        date          not null,
    partner     text          not null check (partner in ('Никита', 'Илья')),
    amount      numeric(14,2) not null check (amount > 0),
    kind        text          not null check (kind in ('вклад', 'выплата')),
    note        text,
    entered_by  text          not null check (fin.valid_author(entered_by)),
    created_at  timestamptz   not null default now()
);
comment on table fin.capital is
    'Вклады и выплаты партнёров — чьи это деньги. Сами деньги на счёте видны в выписке (cff_owner_*), здесь — разметка по партнёрам.';

create table if not exists fin.cash_snapshots (
    id          bigserial primary key,
    date        date          not null,
    balance     numeric(14,2) not null,
    source      text,
    note        text,
    entered_by  text          not null check (fin.valid_author(entered_by)),
    created_at  timestamptz   not null default now()
);
comment on table fin.cash_snapshots is 'Остаток по выписке на дату, внесённый руками, — для сверки с остатком из банка.';

-- Справочник: SKU первой закупки. bot_name + sale_pack_g связывают строку с
-- позицией заказа бота («Те Гуань Инь 100 г»), gtin — когда он появится.
create table if not exists fin.sku (
    sku_name            text primary key,
    category            text,
    supplier_unit       text,
    unit_weight_kg      numeric(10,3) not null check (unit_weight_kg > 0),
    price_per_unit_base numeric(12,2) not null check (price_per_unit_base >= 0),
    qty_units_first_purchase numeric(10,3),
    sale_pack_g         numeric(10,1) not null check (sale_pack_g > 0),
    retail_price        numeric(12,2),
    role                text,
    gtin                text,
    bot_name            text,
    active              boolean not null default true,
    entered_by          text    not null check (fin.valid_author(entered_by)),
    created_at          timestamptz not null default now()
);

create table if not exists fin.bundles (
    bundle        text primary key,
    role          text,
    retail_price  numeric(12,2),
    pouches       integer not null check (pouches > 0),
    pouch_type    text    not null check (pouch_type in ('35g', '100g')),
    codes_total   integer not null check (codes_total >= 0),
    labels_total  integer not null check (labels_total >= 0),
    gtin          text,
    bot_name      text,
    active        boolean not null default true,
    entered_by    text    not null check (fin.valid_author(entered_by)),
    created_at    timestamptz not null default now()
);

create table if not exists fin.bundle_items (
    bundle    text not null references fin.bundles (bundle) on update cascade,
    sku_name  text not null references fin.sku (sku_name) on update cascade,
    grams     numeric(10,1) not null check (grams > 0),
    primary key (bundle, sku_name)
);

-- Партии закупки. Себестоимость грамма — средневзвешенная по партиям
-- (fin.v_tea_cost); пока партий нет — базовый прайс из fin.sku, как в v1.5.
create table if not exists fin.purchase_lots (
    id              bigserial primary key,
    date            date          not null,
    sku_name        text          not null references fin.sku (sku_name) on update cascade,
    supplier        text,
    qty_units       numeric(12,3) not null check (qty_units > 0),
    price_per_unit  numeric(12,2) not null check (price_per_unit >= 0),
    discount        numeric(6,4)  not null default 0 check (discount >= 0 and discount < 1),
    delivery_cost   numeric(12,2) not null default 0 check (delivery_cost >= 0),
    note            text,
    entered_by      text          not null check (fin.valid_author(entered_by)),
    created_at      timestamptz   not null default now()
);
comment on column fin.purchase_lots.qty_units is 'В единицах поставщика: кг для рассыпного, штуки для блинов (вес единицы — fin.sku.unit_weight_kg).';
comment on column fin.purchase_lots.delivery_cost is 'Входящая доставка этой партии, ₽; делится на граммы партии.';

-- Фасовка: из grams_in граммов вышло packs_out пачек. Для сета или набора
-- product — его название, grams_in — пусто (чай уже в пачках).
create table if not exists fin.packing_runs (
    id          bigserial primary key,
    date        date          not null,
    product     text          not null,
    grams_in    numeric(12,1) check (grams_in > 0),
    packs_out   integer       not null check (packs_out > 0),
    note        text,
    entered_by  text          not null check (fin.valid_author(entered_by)),
    created_at  timestamptz   not null default now()
);

create table if not exists fin.ad_spend (
    id           bigserial primary key,
    date         date          not null,
    channel      text          not null default 'VK Реклама',
    spend_own    numeric(12,2) not null default 0 check (spend_own >= 0),
    spend_bonus  numeric(12,2) not null default 0 check (spend_bonus >= 0),
    impressions  integer check (impressions >= 0),
    clicks       integer check (clicks >= 0),
    note         text,
    entered_by   text          not null check (fin.valid_author(entered_by)),
    created_at   timestamptz   not null default now()
);
comment on table fin.ad_spend is 'Расход рекламы по дням (из кабинета, руками). Пополнение кабинета — в выписке; здесь — сколько потрачено.';

create table if not exists fin.plan (
    id          bigserial primary key,
    month       integer       not null check (month between 0 and 24),
    metric      text          not null,
    value       numeric(14,2) not null,
    note        text,
    entered_by  text          not null check (fin.valid_author(entered_by)),
    created_at  timestamptz   not null default now()
);
comment on column fin.plan.month is 'Месяц пилота: 1 — первый месяц от pilot_start_date. Последняя строка по (month, metric) действует.';

-- Действующая вводная на дату: последняя строка по key с valid_from <= даты.
create or replace view fin.inputs_current as
select distinct on (key)
       key, label, value, value_text, unit, status, note, valid_from, entered_by, created_at
from fin.inputs
where valid_from <= current_date
order by key, valid_from desc, created_at desc;
comment on view fin.inputs_current is 'Вводные, действующие сегодня. Лист «1. Вводные».';

create or replace function fin.inp(k text, on_date date default current_date) returns numeric
    language sql stable as $$
    select value from fin.inputs
    where key = k and valid_from <= on_date
    order by valid_from desc, created_at desc
    limit 1
$$;
comment on function fin.inp(text, date) is 'Числовая вводная на дату; нет строки или пусто — NULL («нет данных», не 0).';

create or replace function fin.inp_date(k text, on_date date default current_date) returns date
    language sql stable as $$
    select nullif(value_text, '')::date from fin.inputs
    where key = k and valid_from <= on_date
    order by valid_from desc, created_at desc
    limit 1
$$;

insert into fin.schema_migrations (version) values ('001_schema') on conflict do nothing;
