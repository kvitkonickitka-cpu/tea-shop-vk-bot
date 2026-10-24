"""Таблицы склада в схеме shop: партии, резервы, журнал движений.

Как и каталог, создаёт бот при старте, идемпотентно (`database._connect`).
Финмодель видит партии представлением `fin.purchase_lots` (миграция 014),
поэтому партию вносят один раз — формой «Партия закупки», в эту таблицу.

- **Остаток партии** = приход (`lots.qty`) + корректировки инвентаризации −
  списания при сборке − потери фасовки − порча. Ведёт его журнал
  `stock_moves`, поля «остаток» нет — его нельзя «поправить» мимо журнала.
- **Резерв** — строка `reservations` на каждую пачку заказа (у сета и набора —
  на каждое вложение): какая партия, сколько, в каком состоянии. Уникальна по
  месту в заказе, поэтому повторное уведомление об оплате не зарезервирует
  дважды. Свободно = остаток − резерв.
- Блин 357 г — партия в штуках: резерв и списание по 1 шт.
"""

from __future__ import annotations

UNITS = ("г", "шт")
# Виды движений. Резерв и его снятие тоже пишутся в журнал — чтобы по партии
# было видно всё, что с ней происходило; на остаток они не влияют.
MOVE_KINDS = ("резерв", "снятие резерва", "списание", "потери", "возврат", "корректировка", "порча")
# Что формы Metabase могут записать в журнал сами. Резерв, списание при сборке
# и потери фасовки пишет только бот — иначе остаток разошёлся бы с заказами.
FORM_KINDS = ("корректировка", "порча", "возврат")

DDL = [
    """create table if not exists shop.lots (
        id                   bigserial primary key,
        -- Короткий код партии печатается на этикетке: L-261101-TGY.
        lot_code             text        not null unique,
        raw_material         text        not null,
        unit                 text        not null default 'г',
        qty                  numeric(12,1) not null,
        arrival_date         date        not null default current_date,
        manufacture_date     date,
        best_before          date,
        manufacturer         text,
        manufacturer_address text,
        country              text        default 'Китай',
        importer             text,
        supplier             text,
        -- Декларация поставщика — справочно: пачку декларирует ИП как изготовитель.
        supplier_declaration text,
        invoice_ref          text,
        -- Цена — за килограмм рассыпного или за штуку блина, как в прайсе.
        price_per_unit       numeric(12,2),
        discount             numeric(6,4) not null default 0,
        delivery_cost        numeric(12,2) not null default 0,
        note                 text,
        entered_by           text        not null default session_user,
        active               boolean     not null default true,
        created_at           timestamptz not null default now(),
        updated_at           timestamptz not null default now(),
        constraint lots_unit check (unit in ('г', 'шт')),
        constraint lots_qty check (qty > 0),
        constraint lots_discount check (discount >= 0 and discount < 1)
    )""",
    "create index if not exists lots_raw on shop.lots (raw_material, best_before nulls last, arrival_date)",
    """create table if not exists shop.reservations (
        id               bigserial primary key,
        order_id         integer     not null,
        item_index       integer     not null,
        -- Номер пачки в позиции и вложения в наборе: место резерва в заказе.
        unit_no          integer     not null,
        part_no          integer     not null default 0,
        sku_id           text        not null,
        raw_material     text        not null,
        -- Пусто: свободного сырья не хватило — заказ оформлен, в Ops тревога.
        lot_id           bigint      references shop.lots (id),
        qty              numeric(12,1) not null,
        status           text        not null default 'резерв',
        pack_best_before date,
        packed_at        timestamptz,
        packed_by        text,
        created_at       timestamptz not null default now(),
        updated_at       timestamptz not null default now(),
        constraint reservations_status check (status in ('резерв', 'списан', 'снят')),
        constraint reservations_slot unique (order_id, item_index, unit_no, part_no)
    )""",
    "create index if not exists reservations_lot on shop.reservations (lot_id) where status = 'резерв'",
    """create table if not exists shop.stock_moves (
        id             bigserial primary key,
        lot_id         bigint      references shop.lots (id),
        kind           text        not null,
        -- В единицах партии. У корректировки — со знаком, у остальных — больше нуля.
        qty            numeric(12,1) not null,
        reservation_id bigint      references shop.reservations (id),
        order_id       integer,
        note           text,
        created_by     text        not null default session_user,
        created_at     timestamptz not null default now(),
        constraint stock_moves_kind check (kind in
            ('резерв', 'снятие резерва', 'списание', 'потери', 'возврат', 'корректировка', 'порча')),
        constraint stock_moves_qty check (kind = 'корректировка' or qty > 0)
    )""",
    "create index if not exists stock_moves_lot on shop.stock_moves (lot_id, kind)",
    # Остаток и резерв по партии. Остаток — только из прихода и журнала.
    """create or replace view shop.v_lot_balance as
    select l.id as lot_id, l.lot_code, l.raw_material, l.unit, l.qty as qty_in,
           l.arrival_date, l.manufacture_date, l.best_before, l.active,
           l.qty + coalesce(m.adjusted, 0) - coalesce(m.used, 0) as on_hand,
           coalesce(r.reserved, 0) as reserved,
           l.qty + coalesce(m.adjusted, 0) - coalesce(m.used, 0) - coalesce(r.reserved, 0) as free,
           coalesce(m.written_off, 0) as written_off, coalesce(m.lost, 0) as lost,
           coalesce(m.spoiled, 0) as spoiled, coalesce(m.adjusted, 0) as adjusted
    from shop.lots l
    left join (
        select lot_id,
               sum(qty) filter (where kind in ('списание', 'потери', 'порча')) as used,
               sum(qty) filter (where kind = 'корректировка') as adjusted,
               sum(qty) filter (where kind = 'списание') as written_off,
               sum(qty) filter (where kind = 'потери') as lost,
               sum(qty) filter (where kind = 'порча') as spoiled
        from shop.stock_moves group by lot_id
    ) m on m.lot_id = l.id
    left join (
        select lot_id, sum(qty) as reserved from shop.reservations where status = 'резерв' group by lot_id
    ) r on r.lot_id = l.id""",
    # Проверки партии — с понятным текстом для формы Metabase.
    """create or replace function shop.lots_guard() returns trigger
       language plpgsql security definer set search_path = shop, pg_temp as $$
    begin
        new.lot_code := upper(btrim(coalesce(new.lot_code, '')));
        if new.lot_code !~ '^L-[0-9]{6}-[A-Z0-9]{2,10}$' then
            raise exception 'Код партии «%»: нужно L-ГГММДД-СОРТ латиницей, например L-261101-TGY', new.lot_code;
        end if;
        if nullif(btrim(new.raw_material), '') is null then
            raise exception 'Партия %: укажите сырьё', new.lot_code;
        end if;
        if new.unit not in ('г', 'шт') then
            raise exception 'Партия %: единица — г или шт', new.lot_code;
        end if;
        if new.qty is null or new.qty <= 0 then
            raise exception 'Партия %: количество больше нуля', new.lot_code;
        end if;
        if new.best_before is not null and new.manufacture_date is not null
           and new.best_before <= new.manufacture_date then
            raise exception 'Партия %: «годен до» раньше даты производства', new.lot_code;
        end if;
        if tg_op = 'UPDATE' and new.qty < old.qty and exists (
            select 1 from shop.v_lot_balance b where b.lot_id = new.id and b.on_hand - (old.qty - new.qty) < b.reserved) then
            raise exception 'Партия %: после правки прихода остаток меньше резерва — сначала инвентаризация', new.lot_code;
        end if;
        new.updated_at := now();
        return new;
    end $$""",
    "drop trigger if exists lots_guard on shop.lots",
    "create trigger lots_guard before insert or update on shop.lots for each row execute function shop.lots_guard()",
    # Форма Metabase пишет в журнал только инвентаризацию, порчу и возврат.
    """create or replace function shop.stock_moves_guard() returns trigger
       language plpgsql as $$
    begin
        if current_user = 'metabase_fin' and new.kind not in ('корректировка', 'порча', 'возврат') then
            raise exception 'Движение «%» пишет только бот: резерв при оплате, списание и потери при сборке', new.kind;
        end if;
        if new.kind <> 'корректировка' and coalesce(new.qty, 0) <= 0 then
            raise exception 'Количество движения «%» должно быть больше нуля', new.kind;
        end if;
        return new;
    end $$""",
    "drop trigger if exists stock_moves_guard on shop.stock_moves",
    """create trigger stock_moves_guard before insert or update on shop.stock_moves
       for each row execute function shop.stock_moves_guard()""",
    """do $$
    begin
        if exists (select 1 from pg_roles where rolname = 'metabase_fin') then
            grant usage on schema shop to metabase_fin;
            grant select on all tables in schema shop to metabase_fin;
            revoke insert, update, delete, truncate on shop.lots, shop.reservations, shop.stock_moves from metabase_fin;
            grant insert, update on shop.lots to metabase_fin;
            grant insert on shop.stock_moves to metabase_fin;
            grant usage on sequence shop.lots_id_seq, shop.stock_moves_id_seq to metabase_fin;
        end if;
        if exists (select 1 from pg_roles where rolname = 'metabase_ro') then
            grant usage on schema shop to metabase_ro;
            grant select on all tables in schema shop to metabase_ro;
        end if;
    end $$""",
]


async def ensure(conn) -> None:
    """Таблицы склада — при старте бота, после каталога."""
    for statement in DDL:
        await conn.exec_driver_sql(statement)
