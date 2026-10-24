"""Статусы кодов «Честного знака»: журнал событий и выгрузки для ввода в оборот.

Жизнь кода при фасовке под заказ:

    получен (пул из СУЗ) → напечатан → наклеен (пачка собрана: код ↔ строка
    заказа ↔ партия) → введён в оборот («Производство РФ» принят) → продан
    (закрывающий чек) → возвращён | испорчен (до ввода) | выведен из оборота
    (после ввода: брак, просрочка, отзыв партии).

- **Журнал** `shop.code_events` — каждое событие кода строкой: статус, когда,
  кто, заказ, выгрузка. Текущий статус — последнее событие
  (`shop.v_code_status`), поля «статус» у кода нет — его не поправить мимо
  журнала. Пул (`marking_codes`) и привязка к заказу (`order_marking_codes`)
  остаются как есть: где пачка, знают они, что с кодом — журнал.
- **Выгрузка для ввода в оборот** `shop.code_batches` — наклеенные и ещё не
  введённые коды одним списком. Создать её (форма Metabase или `/codes` в Ops)
  — триггер сам кладёт в неё коды; отметить «Введено» — триггер пишет каждому
  коду «введён в оборот». Поэтому форма Metabase и команда бота работают
  одинаково, а роль Metabase в журнал напрямую не пишет.

Схему создаёт бот при старте, как каталог и склад (`database._connect`).
"""

from __future__ import annotations

# Статусы кода и как их называет владелец.
STATUSES = {
    "ordered": "заказан",
    "received": "получен",
    "printed": "напечатан",
    "applied": "наклеен",
    "introduced": "введён в оборот",
    "sold": "продан",
    "returned": "возвращён",
    "spoiled": "испорчен",
    "withdrawn": "выведен из оборота",
}

DDL = [
    """create table if not exists shop.code_events (
        id         bigserial primary key,
        gtin       text        not null,
        serial     text        not null,
        status     text        not null,
        at         timestamptz not null default now(),
        by         text        not null default session_user,
        order_id   integer,
        batch_id   bigint,
        note       text,
        constraint code_events_status check (status in
            ('ordered', 'received', 'printed', 'applied', 'introduced', 'sold', 'returned', 'spoiled', 'withdrawn'))
    )""",
    "create index if not exists code_events_code on shop.code_events (gtin, serial, id desc)",
    "create index if not exists code_events_status_at on shop.code_events (status, at)",
    """create table if not exists shop.code_batches (
        id             bigserial primary key,
        status         text        not null default 'выгружена',
        created_at     timestamptz not null default now(),
        created_by     text        not null default session_user,
        introduced_at  timestamptz,
        introduced_by  text,
        -- Номер документа «Производство РФ» в ЛК ЧЗ — для сверки.
        document       text,
        note           text,
        constraint code_batches_status check (status in ('выгружена', 'введена', 'отменена'))
    )""",
    """create table if not exists shop.code_batch_items (
        batch_id bigint not null references shop.code_batches (id),
        gtin     text   not null,
        serial   text   not null,
        primary key (batch_id, gtin, serial)
    )""",
    # Журнал заведён 24.10.2026: у кодов, что были раньше, событий нет. Один раз
    # (и для каждого нового такого кода) — по их состоянию: пул → получен,
    # скан → наклеен, чек → продан, чек возврата → возвращён.
    """insert into shop.code_events (gtin, serial, status, at, by, note)
    select p.gtin, p.serial, 'received', coalesce(p.imported_at, p.created_at), 'перенос', 'журнал заведён 24.10.2026'
    from marking_codes p
    where p.from_pool
      and not exists (select 1 from shop.code_events e where e.gtin = p.gtin and e.serial = p.serial)""",
    """insert into shop.code_events (gtin, serial, status, at, by, order_id, note)
    select o.gtin, o.serial, x.status, x.at, 'перенос', o.order_id, 'журнал заведён 24.10.2026'
    from order_marking_codes o
    cross join lateral (values
        (1, 'applied', o.scanned_at),
        (2, 'sold', o.sold_at),
        (3, 'returned', o.returned_at)
    ) as x (n, status, at)
    where x.at is not null
      and not exists (select 1 from shop.code_events e
                      where e.gtin = o.gtin and e.serial = o.serial and e.order_id = o.order_id)
    order by o.id, x.n""",
    # Текущий статус кода — последнее событие; заказ — действующая привязка
    # (`order_marking_codes`, кроме возвращённых), пачка — номер по порядку скана.
    """create or replace view shop.v_code_status as
    with last as (
        select distinct on (gtin, serial) gtin, serial, status, at, by, order_id
        from shop.code_events order by gtin, serial, id desc
    ),
    linked as (
        select o.*, row_number() over (partition by o.order_id, o.item_index order by o.id) - 1 as unit_no
        from order_marking_codes o where o.status <> 'returned'
    )
    select l.gtin, l.serial, l.status, l.at as status_at, l.by as status_by,
           coalesce(k.order_id, l.order_id) as order_id, k.item_index, k.unit_no, k.kind, k.raw_code,
           p.code as pool_code, p.imported_at
    from last l
    left join linked k on k.gtin = l.gtin and k.serial = l.serial
    left join marking_codes p on p.gtin = l.gtin and p.serial = l.serial""",
    # Код ↔ партия сырья: через резерв той же пачки заказа (у набора — все вложения).
    """create or replace view shop.v_code_lots as
    select s.gtin, s.serial, s.order_id, s.item_index, s.unit_no, l.lot_code, l.raw_material, r.sku_id
    from shop.v_code_status s
    join shop.reservations r on r.order_id = s.order_id and r.item_index = s.item_index
                             and r.unit_no = s.unit_no and r.status <> 'снят'
    join shop.lots l on l.id = r.lot_id""",
    # Новая выгрузка забирает все наклеенные коды, которых нет в других открытых
    # или введённых выгрузках. Нечего выгружать — ошибка, а не пустой файл.
    """create or replace function shop.code_batches_fill() returns trigger
       language plpgsql security definer set search_path = shop, public, pg_temp as $$
    begin
        insert into shop.code_batch_items (batch_id, gtin, serial)
        select new.id, s.gtin, s.serial from shop.v_code_status s
        where s.status = 'applied'
          and not exists (select 1 from shop.code_batch_items i join shop.code_batches b on b.id = i.batch_id
                          where i.gtin = s.gtin and i.serial = s.serial and b.status in ('выгружена', 'введена'));
        if not found then
            raise exception 'Нечего выгружать: все наклеенные коды уже в выгрузках или введены в оборот';
        end if;
        return null;
    end $$""",
    "drop trigger if exists code_batches_fill on shop.code_batches",
    """create trigger code_batches_fill after insert on shop.code_batches
       for each row execute function shop.code_batches_fill()""",
    # «Введено»: каждому коду выгрузки, который всё ещё наклеен, — событие ввода.
    """create or replace function shop.code_batches_status() returns trigger
       language plpgsql security definer set search_path = shop, public, pg_temp as $$
    begin
        if new.status = old.status then
            return new;
        end if;
        if old.status <> 'выгружена' then
            raise exception 'Выгрузка №% уже %: статус не меняется', old.id, old.status;
        end if;
        if new.status = 'введена' then
            new.introduced_at := coalesce(new.introduced_at, now());
            new.introduced_by := coalesce(nullif(btrim(new.introduced_by), ''), session_user);
            insert into shop.code_events (gtin, serial, status, by, order_id, batch_id, note)
            select s.gtin, s.serial, 'introduced', new.introduced_by, s.order_id, new.id, new.document
            from shop.code_batch_items i join shop.v_code_status s on s.gtin = i.gtin and s.serial = i.serial
            where i.batch_id = new.id and s.status = 'applied';
        end if;
        return new;
    end $$""",
    "drop trigger if exists code_batches_status on shop.code_batches",
    """create trigger code_batches_status before update on shop.code_batches
       for each row execute function shop.code_batches_status()""",
    """do $$
    begin
        if exists (select 1 from pg_roles where rolname = 'metabase_fin') then
            grant usage on schema shop to metabase_fin;
            grant select on all tables in schema shop to metabase_fin;
            revoke insert, update, delete, truncate on shop.code_events, shop.code_batches, shop.code_batch_items
                from metabase_fin;
            grant insert (note, created_by) on shop.code_batches to metabase_fin;
            grant update (status, document, note, introduced_by) on shop.code_batches to metabase_fin;
            grant usage on sequence shop.code_batches_id_seq to metabase_fin;
        end if;
        if exists (select 1 from pg_roles where rolname = 'metabase_ro') then
            grant usage on schema shop to metabase_ro;
            grant select on all tables in schema shop to metabase_ro;
        end if;
    end $$""",
]


async def ensure(conn) -> None:
    """Журнал кодов и выгрузки — при старте бота, после каталога и склада."""
    for statement in DDL:
        await conn.exec_driver_sql(statement)
