-- 012: розничная цена, GTIN и название товара — из каталога бота (shop.catalog).
--
-- Каталог переехал из Google Таблицы в базу бота и правится в Metabase
-- («Служебное» → «Каталог»). Финмодель больше не хранит розничную цену, GTIN
-- и название в каталоге бота: fin.sku остаётся справочником сырья (цена за кг,
-- кратность, роль), fin.bundles — составом сетов и наборов для себестоимости.
-- Цена, по которой считаются маржа и порог на «Пульсе → Товар», — та же, что
-- у бота: разойтись им больше негде.
--
-- Связь (fin.v_catalog_link):
--   пачка и блин — артикул с этим сырьём (shop.catalog.raw_material = fin.sku.sku_name)
--   и фасовкой fin.sku.sale_pack_g;
--   сет и набор — fin.bundles.sku_id, при миграции заполняется по названию.
--
-- Порядок на бою: деплой бота (создаёт схему shop) → импорт каталога из
-- таблицы (scripts/api.sh 'catalog/import-from-sheet?apply=1') — он читает
-- цены из fin.sku и fin.bundles → эта миграция. Без каталога она не
-- применится: цены пропали бы из финмодели.

do $$
begin
    if to_regclass('shop.catalog') is null then
        raise exception 'Нет схемы shop — сначала деплой бота с каталогом в базе';
    end if;
    if not exists (select 1 from shop.catalog) then
        raise exception 'shop.catalog пуст — сначала импорт каталога (scripts/api.sh ''catalog/import-from-sheet?apply=1''), потом эта миграция';
    end if;
end $$;

alter table fin.bundles add column if not exists sku_id text;
comment on column fin.bundles.sku_id is 'Артикул сета или набора в каталоге бота (shop.catalog.sku_id): цена, GTIN и название — оттуда.';
update fin.bundles b set sku_id = c.sku_id
  from shop.catalog c
 where b.sku_id is null and c.kind in ('сет', 'набор') and lower(c.name) = lower(b.bundle);

create or replace view fin.v_catalog_link as
select s.sku_name as product, c.sku_id, c.name, c.price, c.gtin, c.on_sale
from fin.sku s
left join lateral (
    select c.* from shop.catalog c
    where c.raw_material = s.sku_name and c.net_weight_g = s.sale_pack_g
      and c.kind in ('пачка', 'блин') and c.active
    order by c.on_sale desc, c.sku_id
    limit 1
) c on true
union all
select b.bundle, c.sku_id, c.name, c.price, c.gtin, c.on_sale
from fin.bundles b
left join shop.catalog c on c.sku_id = b.sku_id and c.active;
comment on view fin.v_catalog_link is
    'Товар финмодели → артикул каталога бота: цена, GTIN и название берутся оттуда. sku_id пуст — артикула в каталоге нет.';

create or replace function fin.unit_costs(on_date date default current_date)
returns table (
    product text, kind text, grams numeric, retail_price numeric,
    tea_cost numeric, inbound_cost numeric, pouches integer, pouch_cost numeric,
    codes integer, codes_cost numeric, labels integer, labels_cost numeric, box_cost numeric,
    product_cost numeric, order_extra_cost numeric, full_cost numeric,
    margin_before_cac numeric, margin_pct numeric,
    price_floor_cac0 numeric, price_floor_cac_low numeric, price_floor_cac_mid numeric, price_floor_cac_high numeric,
    first_order_after_cac_mid numeric, cost_source text, bot_name text, gtin text
)
language sql stable as $$
with r as (
    select coalesce(fin.inp('packing_loss_rate', on_date), 0) as loss,
           fin.inp('usn_rate', on_date) + fin.payment_rate(on_date) as fees,
           fin.inp('code_price', on_date) as code_price,
           fin.inp('pack_label_cost', on_date) as pack_label,
           fin.inp('label_unit_cost', on_date) as label_unit,
           fin.inp('shipping_label_cost', on_date) as ship_label,
           fin.inp('other_variable_per_order', on_date) as other_var,
           fin.inp('pouch_100g_cost', on_date) as pouch_100,
           fin.inp('pouch_35g_cost', on_date) as pouch_35,
           fin.api_per_new_buyer(on_date) as api,
           coalesce(fin.inp('delivery_subsidy_per_order', on_date), 0) as subsidy,
           -- Лист 3, E / лист 4, W: комиссия и налог с доставки, оплаченной через магазин.
           fin.inp('avg_delivery_paid_via_shop', on_date) * (fin.inp('usn_rate', on_date) + fin.payment_rate(on_date)) as delivery_fees,
           fin.inp('cac_low', on_date) as cac_low, fin.inp('cac_mid', on_date) as cac_mid, fin.inp('cac_high', on_date) as cac_high
),
packs as (
    -- Лист 2: J — продажных единиц из закупочной (блин 357 г — одна), L — чай
    -- на единицу, M — логистика, N — дой-пак (блину не нужен), O — код, P — этикетка.
    select s.sku_name as product,
           case when s.supplier_unit ilike 'пресс%' then 'блин' else 'пачка' end as kind,
           s.sale_pack_g as grams, l.price::numeric as retail_price,
           case when s.supplier_unit ilike 'пресс%' then t.tea_rub_per_g * s.sale_pack_g
                else t.tea_rub_per_g * s.sale_pack_g / (1 - r.loss) end as tea_cost,
           case when s.supplier_unit ilike 'пресс%' then t.inbound_rub_per_g * s.sale_pack_g
                else t.inbound_rub_per_g * s.sale_pack_g / (1 - r.loss) end as inbound_cost,
           case when s.supplier_unit ilike 'пресс%' then 0 else 1 end as pouches,
           case when s.supplier_unit ilike 'пресс%' then 0 else r.pouch_100 end as pouch_cost,
           1 as codes, r.code_price as codes_cost, 1 as labels, r.pack_label as labels_cost, 0::numeric as box_cost,
           -- Лист 3, E: расходы заказа из одной пачки — прочие переменные (с коробкой),
           -- этикетка отправки, Claude API на нового покупателя, субсидия, комиссия с доставки.
           r.other_var + r.ship_label + r.api + r.subsidy + r.delivery_fees as order_extra_cost,
           t.cost_source, l.name as bot_name, l.gtin
    from fin.sku s join fin.v_tea_cost t using (sku_name) cross join r
    left join fin.v_catalog_link l on l.product = s.sku_name
    where s.active
),
sets as (
    -- Лист 4: E, F — чай и логистика состава; G×H — пакеты; I — коробка
    -- (= прочие переменные на заказ, строка 29 вводных); J — коды, K — этикетки,
    -- L — Claude API, W — комиссия с доставки.
    select b.bundle as product,
           case when b.pouch_type = '35g' then 'набор' else 'сет' end as kind,
           sum(i.grams) as grams, l.price::numeric as retail_price,
           sum(i.grams * t.tea_rub_per_g / (1 - r.loss)) as tea_cost,
           sum(i.grams * t.inbound_rub_per_g / (1 - r.loss)) as inbound_cost,
           b.pouches,
           b.pouches * case when b.pouch_type = '35g' then r.pouch_35 else r.pouch_100 end as pouch_cost,
           b.codes_total as codes, b.codes_total * r.code_price as codes_cost,
           b.labels_total as labels, b.labels_total * r.label_unit as labels_cost,
           r.other_var as box_cost,
           r.api + r.subsidy + r.delivery_fees as order_extra_cost,
           min(t.cost_source) as cost_source, l.name as bot_name, l.gtin
    from fin.bundles b
    left join fin.v_catalog_link l on l.product = b.bundle
    join fin.bundle_items i on i.bundle = b.bundle
    join fin.v_tea_cost t on t.sku_name = i.sku_name
    cross join r
    where b.active
    group by b.bundle, l.price, b.pouches, b.pouch_type, b.codes_total, b.labels_total, l.name, l.gtin,
             r.pouch_35, r.pouch_100, r.code_price, r.label_unit, r.other_var, r.api, r.subsidy, r.delivery_fees
),
u as (
    select *, tea_cost + inbound_cost + pouch_cost + codes_cost + labels_cost + box_cost as product_cost from packs
    union all
    select *, tea_cost + inbound_cost + pouch_cost + codes_cost + labels_cost + box_cost from sets
)
select u.product, u.kind, u.grams, u.retail_price,
       u.tea_cost, u.inbound_cost, u.pouches, u.pouch_cost, u.codes, u.codes_cost, u.labels, u.labels_cost, u.box_cost,
       u.product_cost, u.order_extra_cost, u.product_cost + u.order_extra_cost as full_cost,
       -- Лист 3, L / лист 4, R: маржа до CAC = цена × (1 − УСН − платёжная нагрузка) − полная себестоимость.
       u.retail_price * (1 - r.fees) - (u.product_cost + u.order_extra_cost) as margin_before_cac,
       (u.retail_price * (1 - r.fees) - (u.product_cost + u.order_extra_cost)) / nullif(u.retail_price, 0) as margin_pct,
       -- Лист 3, G–J: порог цены при CAC 0 / 300 / 500 / 700.
       (u.product_cost + u.order_extra_cost) / (1 - r.fees),
       (u.product_cost + u.order_extra_cost + r.cac_low) / (1 - r.fees),
       (u.product_cost + u.order_extra_cost + r.cac_mid) / (1 - r.fees),
       (u.product_cost + u.order_extra_cost + r.cac_high) / (1 - r.fees),
       -- Лист 3, N / лист 4, T: заказ после сценарного CAC.
       u.retail_price * (1 - r.fees) - (u.product_cost + u.order_extra_cost) - r.cac_mid,
       u.cost_source, u.bot_name, u.gtin
from u cross join r
$$;
comment on function fin.unit_costs(date) is 'Себестоимость и маржа единицы на дату. Листы 2, 3, 4. Цена, GTIN и название — из каталога бота (fin.v_catalog_link).';

create or replace view fin.v_stock as
with sold as (
    select fin_product as product,
           sum(quantity) filter (where not is_refunded) as sold,
           sum(quantity) filter (where is_refunded) as refunded,
           sum(quantity) filter (where paid_date_msk > current_date - 14) as sold_14d
    from fin.v_order_lines where has_cost group by fin_product
),
packed as (
    select product, sum(packs_out) as packed, sum(grams_in) as grams_in from fin.packing_runs group by product
),
bundle_grams as (
    select i.sku_name, sum(i.grams * r.packs_out) as grams
    from fin.packing_runs r join fin.bundle_items i on i.bundle = r.product group by i.sku_name
),
products as (
    select sku_name as product, case when supplier_unit ilike 'пресс%' then 'блин' else 'пачка' end as kind,
           sku_name as lead_sku from fin.sku where active
    union all
    -- Сет держится на самом долгом в поставке составе — срок по первому SKU состава.
    select b.bundle, case when b.pouch_type = '35g' then 'набор' else 'сет' end,
           (select min(i.sku_name) from fin.bundle_items i where i.bundle = b.bundle) from fin.bundles b where b.active
),
supplier as (
    select distinct on (sku_name) sku_name, supplier from fin.purchase_lots where supplier is not null
    order by sku_name, date desc, created_at desc
),
lead as (
    select p.product,
           coalesce(
               (select t.lead_days from fin.supply_lead_times t where t.scope = 'sku' and t.name = p.lead_sku
                  and t.valid_from <= current_date order by t.valid_from desc, t.created_at desc limit 1),
               (select t.lead_days from fin.supply_lead_times t join supplier s on s.supplier = t.name
                 where t.scope = 'поставщик' and s.sku_name = p.lead_sku and t.valid_from <= current_date
                 order by t.valid_from desc, t.created_at desc limit 1),
               fin.inp('supply_lead_days')::int) as lead_days
    from products p
),
base as (
    select p.product, p.kind,
           t.grams_bought,
           case when p.kind in ('пачка', 'блин') then coalesce(pk.grams_in, 0) + coalesce(bg.grams, 0) end as grams_used,
           case when p.kind in ('пачка', 'блин') then t.grams_bought - coalesce(pk.grams_in, 0) - coalesce(bg.grams, 0) end as grams_left,
           pk.packed, s.sold, s.refunded,
           pk.packed - coalesce(s.sold, 0) - coalesce(s.refunded, 0) as on_hand,
           s.sold_14d / 14.0 as avg_daily_sales_14d,
           ld.lead_days
    from products p
    left join fin.v_tea_cost t on t.sku_name = p.product
    left join packed pk on pk.product = p.product
    left join bundle_grams bg on bg.sku_name = p.product
    left join sold s on s.product = p.product
    left join lead ld on ld.product = p.product
)
select b.product, b.kind, b.grams_bought, b.grams_used, b.grams_left, b.packed, b.sold, b.refunded, b.on_hand,
       b.avg_daily_sales_14d,
       b.on_hand / nullif(b.avg_daily_sales_14d, 0) as days_left,
       coalesce(b.on_hand / nullif(b.avg_daily_sales_14d, 0) < 14, false) as reorder,
       -- новые столбцы (006): точка заказа = (поставка + фасовка + страховой запас) × продажи в день.
       b.lead_days,
       fin.inp('packing_marking_days')::int as packing_days,
       fin.inp('safety_stock_days')::int as safety_days,
       (b.lead_days + fin.inp('packing_marking_days') + fin.inp('safety_stock_days')) * coalesce(b.avg_daily_sales_14d, 0)
           as reorder_point,
       case when b.lead_days is null or fin.inp('packing_marking_days') is null or fin.inp('safety_stock_days') is null
                then null
            else coalesce(b.on_hand, 0) <= (b.lead_days + fin.inp('packing_marking_days') + fin.inp('safety_stock_days'))
                                          * coalesce(b.avg_daily_sales_14d, 0)
                 and coalesce(b.avg_daily_sales_14d, 0) > 0 end as order_now,
       -- Бот товар не продаёт: артикула нет или он не в продаже (shop.catalog.on_sale) —
       -- дни запаса всё равно считаем.
       not exists (select 1 from fin.v_catalog_link l where l.product = b.product and l.on_sale) as not_in_bot
from base b;
comment on view fin.v_stock is
    'Склад: граммы, пачки и сеты на руках, дни запаса, точка заказа = (срок поставки + фасовка + страховой запас) × продажи в день за 14 дней; order_now — пора заказывать. Пусто — нет данных (вводные supply_lead_days, packing_marking_days, safety_stock_days).';

-- Розничная цена, GTIN и название в боте больше не живут в финмодели.
alter table fin.sku drop column retail_price, drop column gtin, drop column bot_name;
alter table fin.bundles drop column retail_price, drop column gtin, drop column bot_name;

insert into fin.schema_migrations (version) values ('012_catalog_prices') on conflict do nothing;
