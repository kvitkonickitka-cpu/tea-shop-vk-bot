"""Каталог в базе: схема `shop`, её проверки и журнал изменений.

Каталог правят в Metabase формами, а иногда — руками в psql. Поэтому всё,
что должно держаться при любой правке, держит сама база, а не форма:

- **проверки** — в триггере до записи, с понятным текстом: Metabase
  показывает владельцу сообщение исключения как есть. Ограничения CHECK
  рядом — страховка на случай, если триггер кто-то отключит;
- **журнал** — триггер после записи кладёт каждое изменённое поле в
  `shop.catalog_changes` (кто, когда, было → стало). Функция с правами
  владельца схемы: у роли Metabase на журнал только чтение, подделать или
  стереть историю формой нельзя;
- **удаления нет**: снять с продажи — `on_sale`, убрать совсем — `active`.
  У роли Metabase права DELETE нет, а внешние ключи состава не дают удалить
  артикул, который лежит в наборе.

Схему создаёт бот при старте (`database._connect`), как схему analytics:
идемпотентно, своей транзакцией. Финмодель (`analytics/fin`) читает её
представлениями — поэтому миграции `fin`, которые на неё ссылаются,
применяются после деплоя бота.
"""

from __future__ import annotations

SCHEMA = "shop"

# Виды артикула. Пачка и блин фасуются из сырья, сет и набор собираются из
# других артикулов (`shop.bundle_items`) и сырья своего не имеют.
KINDS = ("пачка", "блин", "сет", "набор")
PACKED = ("пачка", "блин")
BUNDLES = ("сет", "набор")

DDL = [
    "create schema if not exists shop",
    # Контрольная цифра GS1: веса 3 и 1 справа налево, без последней цифры.
    # GTIN-13 дополняем нулём слева — для GS1 это тот же номер.
    """create or replace function shop.gtin_ok(g text) returns boolean
       language sql immutable as $$
        select g ~ '^[0-9]{13,14}$' and (
            select (10 - sum(substr(p, i, 1)::int * case when i % 2 = 1 then 3 else 1 end) % 10) % 10
              from (select lpad(g, 14, '0') as p) x, generate_series(1, 13) i
        ) = right(g, 1)::int
    $$""",
    """create table if not exists shop.catalog (
        -- Числовой ключ — для Metabase: форма «Обновить» в карточке строки ищет
        -- ключ с именем id и с текстовым ключом не открывается. Артикул — sku_id,
        -- уникальный, на него ссылаются состав наборов и заказы.
        id                bigserial primary key,
        sku_id            text        not null unique,
        kind              text        not null,
        name              text        not null,
        description       text        not null default '',
        -- Фасовка в граммах. У сета и набора пусто: вес — сумма вложений,
        -- его считает бот по составу.
        net_weight_g      numeric(8,1),
        -- Из какого сырья фасуется: название из справочника сырья финмодели
        -- (`fin.sku.sku_name`). Одно сырьё — несколько фасовок.
        raw_material      text,
        gtin              text,
        -- Пусто только у артикула, который отдельно не продаётся: пакетик
        -- 35 г внутри набора.
        price             integer,
        on_sale           boolean     not null default false,
        vk_market_url     text,
        recommend_with    text[]      not null default '{}',
        brew_guide        text        not null default '',
        video_url         text,
        is_new            boolean     not null default false,
        synonyms          text[]      not null default '{}',
        sort_order        integer     not null default 100,
        category          text,
        label_name        text,
        label_kind        text,
        storage           text,
        shelf_life_months integer,
        qr_url            text,
        active            boolean     not null default true,
        updated_at        timestamptz not null default now(),
        updated_by        text        not null default session_user,
        -- Причина правки — в журнал этой правки; подтверждение большого
        -- изменения цены — на одну запись, триггер его сбрасывает.
        change_note       text,
        price_confirmed   boolean     not null default false,
        constraint catalog_kind check (kind in ('пачка', 'блин', 'сет', 'набор')),
        constraint catalog_price check (price is null or price > 0),
        constraint catalog_gtin check (gtin is null or shop.gtin_ok(gtin)),
        constraint catalog_sale check (not on_sale or (active and gtin is not null and price is not null))
    )""",
    # Каталог, созданный до 24.10.2026, — с ключом sku_id: переводим на id, внешние
    # ключи состава переставляем на уникальный sku_id.
    "alter table shop.catalog add column if not exists id bigserial",
    """do $$
    begin
        if exists (select 1 from pg_constraint c join pg_attribute a
                     on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
                   where c.conrelid = 'shop.catalog'::regclass and c.contype = 'p' and a.attname = 'sku_id') then
            alter table shop.catalog add constraint catalog_sku_unique unique (sku_id);
            if to_regclass('shop.bundle_items') is not null then
                alter table shop.bundle_items drop constraint if exists bundle_items_bundle_sku_fkey,
                                              drop constraint if exists bundle_items_item_sku_fkey;
            end if;
            alter table shop.catalog drop constraint catalog_pkey;
            alter table shop.catalog add constraint catalog_pkey primary key (id);
            if to_regclass('shop.bundle_items') is not null then
                alter table shop.bundle_items
                    add constraint bundle_items_bundle_sku_fkey foreign key (bundle_sku) references shop.catalog (sku_id),
                    add constraint bundle_items_item_sku_fkey foreign key (item_sku) references shop.catalog (sku_id);
            end if;
        end if;
    end $$""",
    "create unique index if not exists catalog_gtin_unique on shop.catalog (gtin) where gtin is not null",
    # Бот находит товар по названию: среди действующих оно должно быть одно.
    "create unique index if not exists catalog_name_unique on shop.catalog (lower(name)) where active",
    """create table if not exists shop.bundle_items (
        -- Отдельный ключ, а не пара артикулов: Metabase даёт форме «Исправить»
        -- только таблицы с ключом из одного столбца.
        id          bigserial primary key,
        bundle_sku  text        not null references shop.catalog (sku_id),
        item_sku    text        not null references shop.catalog (sku_id),
        quantity    integer     not null default 1,
        active      boolean     not null default true,
        updated_at  timestamptz not null default now(),
        updated_by  text        not null default session_user,
        change_note text,
        constraint bundle_items_quantity check (quantity > 0),
        constraint bundle_items_pair unique (bundle_sku, item_sku)
    )""",
    """create table if not exists shop.catalog_changes (
        id          bigserial primary key,
        changed_at  timestamptz not null default now(),
        sku_id      text        not null,
        entity      text        not null,
        action      text        not null,
        field       text,
        old_value   text,
        new_value   text,
        changed_by  text,
        note        text,
        -- Сообщение в Ops ушло (или не нужно): ставит тик, условным UPDATE.
        notified_at timestamptz
    )""",
    "create index if not exists catalog_changes_unnotified on shop.catalog_changes (id) where notified_at is null",
    "create index if not exists catalog_changes_sku on shop.catalog_changes (sku_id, id desc)",
    # Проверки и журнал артикула. Security definer: роль Metabase пишет в
    # каталог, но не в журнал.
    """create or replace function shop.catalog_guard() returns trigger
       language plpgsql security definer set search_path = shop, pg_temp as $$
    declare
        bad text;
    begin
        if tg_op = 'UPDATE' and new.sku_id is distinct from old.sku_id then
            raise exception 'Артикул % нельзя переименовать: на код ссылаются заказы и коды маркировки. Заведите новый артикул, а этот снимите (active = нет)', old.sku_id;
        end if;
        if new.sku_id is null or new.sku_id !~ '^[A-Za-z0-9][A-Za-z0-9-]{1,39}$' then
            raise exception 'Артикул «%»: только латиница, цифры и дефис, до 40 знаков (например, P-TGI-ECO-100)', new.sku_id;
        end if;
        if new.kind is null or new.kind not in ('пачка', 'блин', 'сет', 'набор') then
            raise exception 'Артикул %: тип «%» — нужно пачка, блин, сет или набор', new.sku_id, new.kind;
        end if;
        new.name := btrim(coalesce(new.name, ''));
        if new.name = '' or length(new.name) > 80 then
            raise exception 'Артикул %: название обязательно и не длиннее 80 символов (сейчас %)', new.sku_id, length(new.name);
        end if;
        new.description := coalesce(new.description, '');
        if length(new.description) > 1500 then
            raise exception 'Артикул %: описание длиннее 1500 символов (%)', new.sku_id, length(new.description);
        end if;
        new.brew_guide := coalesce(new.brew_guide, '');
        if length(new.brew_guide) > 1000 then
            raise exception 'Артикул %: «Как заваривать» длиннее 1000 символов (%)', new.sku_id, length(new.brew_guide);
        end if;
        if new.kind in ('пачка', 'блин') then
            if new.net_weight_g is null or new.net_weight_g <= 0 then
                raise exception 'Артикул %: у пачки и блина нужна фасовка в граммах больше нуля', new.sku_id;
            end if;
            if nullif(btrim(new.raw_material), '') is null then
                raise exception 'Артикул %: у пачки и блина нужно сырьё — из чего фасуем', new.sku_id;
            end if;
        elsif new.net_weight_g is not null and new.net_weight_g <= 0 then
            raise exception 'Артикул %: вес должен быть больше нуля', new.sku_id;
        end if;
        new.gtin := nullif(btrim(new.gtin), '');
        if new.gtin is not null and not shop.gtin_ok(new.gtin) then
            raise exception 'Артикул %: GTIN «%» — нужно 13 или 14 цифр с верной контрольной цифрой', new.sku_id, new.gtin;
        end if;
        if new.price is not null and new.price <= 0 then
            raise exception 'Артикул %: цена должна быть целым числом больше нуля', new.sku_id;
        end if;
        if new.on_sale and not new.active then
            raise exception 'Артикул % в архиве — его нельзя включить в продажу', new.sku_id;
        end if;
        if new.on_sale and new.gtin is null then
            raise exception 'Артикул %: без GTIN включить в продажу нельзя — без него пачку не собрать с кодом маркировки', new.sku_id;
        end if;
        if new.on_sale and new.price is null then
            raise exception 'Артикул %: без цены включить в продажу нельзя', new.sku_id;
        end if;
        new.vk_market_url := nullif(btrim(new.vk_market_url), '');
        if new.vk_market_url is not null and new.vk_market_url !~ '^https://(m\\.)?vk\\.(ru|com)/market' then
            raise exception 'Артикул %: ссылка на Маркет ВК должна начинаться с https://vk.ru/market или https://vk.com/market', new.sku_id;
        end if;
        new.video_url := nullif(btrim(new.video_url), '');
        if new.video_url is not null and new.video_url !~ '^https://\\S+$' then
            raise exception 'Артикул %: ссылка на видео — только https://…', new.sku_id;
        end if;
        new.qr_url := nullif(btrim(new.qr_url), '');
        if new.qr_url is not null and new.qr_url !~ '^https://\\S+$' then
            raise exception 'Артикул %: ссылка для QR — только https://…', new.sku_id;
        end if;
        if new.shelf_life_months is not null and new.shelf_life_months <= 0 then
            raise exception 'Артикул %: срок годности в месяцах должен быть больше нуля', new.sku_id;
        end if;
        -- «С чем советуем»: только существующие действующие артикулы, без
        -- самого себя и без повторов.
        new.recommend_with := coalesce(
            (select array_agg(distinct btrim(r)) from unnest(new.recommend_with) r where btrim(r) <> ''),
            '{}');
        select string_agg(r, ', ') into bad from unnest(new.recommend_with) r
         where r = new.sku_id or not exists (select 1 from shop.catalog c where c.sku_id = r and c.active);
        if bad is not null then
            raise exception 'Артикул %: в «С чем советуем» нет таких действующих артикулов (или это он сам): %', new.sku_id, bad;
        end if;
        new.synonyms := coalesce(
            (select array_agg(btrim(s)) from unnest(new.synonyms) s where btrim(s) <> ''), '{}');
        if tg_op = 'UPDATE' and old.price is not null and new.price is not null
           and new.price <> old.price and abs(new.price - old.price) > 0.3 * old.price
           and not new.price_confirmed then
            raise exception 'Артикул %: цена меняется больше чем на 30 %% (было %, стало %). Если всё верно — отметьте «Да, цена верная»', new.sku_id, old.price, new.price;
        end if;

        if tg_op = 'INSERT' then
            new.updated_by := coalesce(nullif(btrim(new.updated_by), ''), session_user);
        else
            -- Причина и автор — только этой правки: то же значение, что лежит
            -- с прошлой, значит, их сейчас не указали. Автор тогда — роль базы:
            -- прежнее имя в updated_by не значит, что правил он.
            if new.change_note is not distinct from old.change_note then
                new.change_note := null;
            end if;
            new.updated_by := coalesce(
                case when new.updated_by is distinct from old.updated_by then nullif(btrim(new.updated_by), '') end,
                session_user);
            if (to_jsonb(new) - array['updated_at', 'updated_by', 'change_note', 'price_confirmed'])
               = (to_jsonb(old) - array['updated_at', 'updated_by', 'change_note', 'price_confirmed']) then
                -- Ничего не поменялось — ни журнала, ни новой отметки времени.
                new.updated_at := old.updated_at;
                new.updated_by := old.updated_by;
                new.price_confirmed := false;
                return new;
            end if;
        end if;
        new.updated_at := now();
        new.price_confirmed := false;
        return new;
    end $$""",
    "drop trigger if exists catalog_guard on shop.catalog",
    """create trigger catalog_guard before insert or update on shop.catalog
       for each row execute function shop.catalog_guard()""",
    # Журнал — после записи: при INSERT … ON CONFLICT строка, которая уже
    # была, не должна появиться в журнале как «добавлена».
    """create or replace function shop.catalog_journal() returns trigger
       language plpgsql security definer set search_path = shop, pg_temp as $$
    declare
        k text;
        old_j jsonb;
        new_j jsonb;
    begin
        if tg_op = 'INSERT' then
            insert into shop.catalog_changes (sku_id, entity, action, new_value, changed_by, note)
            values (new.sku_id, 'артикул', 'добавлен', new.name, new.updated_by, new.change_note);
            return null;
        end if;
        old_j := to_jsonb(old) - array['updated_at', 'updated_by', 'change_note', 'price_confirmed'];
        new_j := to_jsonb(new) - array['updated_at', 'updated_by', 'change_note', 'price_confirmed'];
        for k in select key from jsonb_each(new_j) where new_j -> key is distinct from old_j -> key loop
            insert into shop.catalog_changes (sku_id, entity, action, field, old_value, new_value, changed_by, note)
            values (new.sku_id, 'артикул', 'изменён', k, old_j ->> k, new_j ->> k, new.updated_by, new.change_note);
        end loop;
        return null;
    end $$""",
    "drop trigger if exists catalog_journal on shop.catalog",
    """create trigger catalog_journal after insert or update on shop.catalog
       for each row execute function shop.catalog_journal()""",
    """create or replace function shop.bundle_items_guard() returns trigger
       language plpgsql security definer set search_path = shop, pg_temp as $$
    declare
        bundle_kind text;
        item_kind text;
    begin
        select kind into bundle_kind from shop.catalog where sku_id = new.bundle_sku;
        select kind into item_kind from shop.catalog where sku_id = new.item_sku;
        if bundle_kind is null or bundle_kind not in ('сет', 'набор') then
            raise exception 'Состав: % — не сет и не набор', new.bundle_sku;
        end if;
        if item_kind is null or item_kind not in ('пачка', 'блин') then
            raise exception 'Состав %: вложение % — не пачка и не блин', new.bundle_sku, new.item_sku;
        end if;
        if new.quantity is null or new.quantity <= 0 then
            raise exception 'Состав %: количество % должно быть больше нуля', new.bundle_sku, new.item_sku;
        end if;
        if tg_op = 'UPDATE' and (new.bundle_sku, new.item_sku) is distinct from (old.bundle_sku, old.item_sku) then
            raise exception 'Состав %: вложение не меняют — добавьте новое, а это выключите (active = нет)', old.bundle_sku;
        end if;
        if tg_op = 'INSERT' then
            new.updated_by := coalesce(nullif(btrim(new.updated_by), ''), session_user);
        else
            if new.change_note is not distinct from old.change_note then
                new.change_note := null;
            end if;
            if (new.quantity, new.active) is not distinct from (old.quantity, old.active) then
                new.updated_at := old.updated_at;
                new.updated_by := old.updated_by;
                return new;
            end if;
            new.updated_by := coalesce(
                case when new.updated_by is distinct from old.updated_by then nullif(btrim(new.updated_by), '') end,
                session_user);
        end if;
        new.updated_at := now();
        return new;
    end $$""",
    "drop trigger if exists bundle_items_guard on shop.bundle_items",
    """create trigger bundle_items_guard before insert or update on shop.bundle_items
       for each row execute function shop.bundle_items_guard()""",
    """create or replace function shop.bundle_items_journal() returns trigger
       language plpgsql security definer set search_path = shop, pg_temp as $$
    begin
        if tg_op = 'INSERT' then
            insert into shop.catalog_changes (sku_id, entity, action, field, new_value, changed_by, note)
            values (new.bundle_sku, 'состав', 'добавлен', new.item_sku, new.quantity::text, new.updated_by, new.change_note);
        elsif (new.quantity, new.active) is distinct from (old.quantity, old.active) then
            insert into shop.catalog_changes (sku_id, entity, action, field, old_value, new_value, changed_by, note)
            values (new.bundle_sku, 'состав', 'изменён', new.item_sku,
                    case when old.active then old.quantity::text else 'выключено' end,
                    case when new.active then new.quantity::text else 'выключено' end,
                    new.updated_by, new.change_note);
        end if;
        return null;
    end $$""",
    "drop trigger if exists bundle_items_journal on shop.bundle_items",
    """create trigger bundle_items_journal after insert or update on shop.bundle_items
       for each row execute function shop.bundle_items_journal()""",
    # Права. Ролей Metabase нет в тестах и на чужой базе — тогда ничего.
    # metabase_fin — формы: добавить и исправить каталог и состав, без
    # удаления; журнал только читать. metabase_ro — дашборды, только чтение.
    """do $$
    begin
        if exists (select 1 from pg_roles where rolname = 'metabase_fin') then
            grant usage on schema shop to metabase_fin;
            grant select on all tables in schema shop to metabase_fin;
            revoke insert, update, delete, truncate on shop.catalog, shop.bundle_items, shop.catalog_changes from metabase_fin;
            grant insert, update on shop.catalog, shop.bundle_items to metabase_fin;
            grant usage on sequence shop.bundle_items_id_seq, shop.catalog_id_seq to metabase_fin;
        end if;
        if exists (select 1 from pg_roles where rolname = 'metabase_ro') then
            grant usage on schema shop to metabase_ro;
            grant select on all tables in schema shop to metabase_ro;
        end if;
    end $$""",
]


async def ensure(conn) -> None:
    """Схема каталога — при старте бота, своей транзакцией."""
    for statement in DDL:
        await conn.exec_driver_sql(statement)
