-- 008: «Честный знак» — ввод в оборот партиями и списание испорченных этикеток (пакет E).
--
-- Интеграции с ГИС МТ и подписи УКЭП в коде нет, поэтому ввод в оборот —
-- ручной шаг: бот выгружает список кодов партии для личного кабинета ЧЗ
-- (/codes в чате Ops), владельцы подают его в ЛК и отмечают статус партии
-- («подан», «принят») — командой /circulation или формой Metabase.
-- Код в закрывающем чеке должен быть из принятой партии — это проверяют
-- контроли (B1) и закрытие месяца (B3). Пул кодов (public.marking_codes)
-- миграция не меняет: партии и списания ссылаются на него по id.

create table if not exists fin.code_batches (
    id             bigserial primary key,
    gtin           text          not null,
    product        text,
    codes_count    integer       not null check (codes_count > 0),
    status         text          not null default 'не подан' check (status in ('не подан', 'подан', 'принят')),
    submitted_at   date,
    accepted_at    date,
    packing_run_id bigint references fin.packing_runs (id) on delete set null,
    note           text,
    entered_by     text          not null check (fin.valid_author(entered_by)),
    created_at     timestamptz   not null default now()
);
comment on table fin.code_batches is
    'Партия кодов ЧЗ на ввод в оборот: напечатана и нанесена → подана в ЛК ЧЗ → принята. Список кодов — fin.code_batch_items.';

create table if not exists fin.code_batch_items (
    batch_id  bigint not null references fin.code_batches (id) on delete cascade,
    code_id   bigint not null unique,
    gtin      text   not null,
    serial    text   not null,
    primary key (batch_id, code_id)
);
comment on column fin.code_batch_items.code_id is 'public.marking_codes.id — код из пула СУЗ; один код — в одной партии.';

create table if not exists fin.code_writeoffs (
    id          bigserial primary key,
    date        date        not null default current_date,
    code_id     bigint,
    gtin        text        not null,
    serial      text        not null,
    reason      text        not null check (reason in ('испорчена этикетка', 'брак печати', 'утеря', 'другое')),
    note        text,
    entered_by  text        not null check (fin.valid_author(entered_by)),
    created_at  timestamptz not null default now(),
    unique (gtin, serial)
);
comment on table fin.code_writeoffs is 'Списанные коды ЧЗ (испорченная этикетка, брак печати, утеря) — с причиной; в заказ такой код попадать не должен.';

-- Коды по GTIN: прежние столбцы (printed теперь — в партиях), новые — в конец.
create or replace view fin.v_codes as
select m.gtin,
       count(*) filter (where m.from_pool) as imported,
       count(*) filter (where m.status in ('assigned', 'sold')) as used_in_orders,
       count(*) filter (where m.status = 'in_stock') as available,
       count(*) filter (where m.status = 'returned') as returned,
       count(*) filter (where i.code_id is not null) as printed,
       count(*) filter (where b.status = 'подан') as submitted,
       count(*) filter (where b.status = 'принят') as in_circulation,
       count(*) filter (where w.id is not null) as written_off,
       count(*) filter (where m.status in ('assigned', 'sold') and coalesce(b.status, '') <> 'принят') as sold_not_in_circulation
from public.marking_codes m
left join fin.code_batch_items i on i.code_id = m.id
left join fin.code_batches b on b.id = i.batch_id
left join fin.code_writeoffs w on w.gtin = m.gtin and w.serial = m.serial
group by m.gtin;
comment on view fin.v_codes is
    'Коды ЧЗ по GTIN: из СУЗ, в заказах, свободно, вернулось, напечатано (в партиях), подано, в обороте (принято), списано, в заказах без ввода в оборот.';

create or replace view fin.v_code_batches as
select b.id, b.created_at, b.gtin, b.product, b.codes_count, b.status, b.submitted_at, b.accepted_at,
       b.packing_run_id, b.note, b.entered_by,
       (select count(*) from fin.code_batch_items i join public.marking_codes m on m.id = i.code_id
         where i.batch_id = b.id and m.status in ('assigned', 'sold')) as used_in_orders
from fin.code_batches b order by b.id desc;
comment on view fin.v_code_batches is 'Партии кодов на ввод в оборот: статус в ЛК ЧЗ и сколько кодов партии уже в заказах.';

insert into fin.schema_migrations (version) values ('008_marking_circulation') on conflict do nothing;
