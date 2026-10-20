-- 003: формулы финмодели v1.5 — функциями и представлениями.
--
-- Ссылки «лист / строка» — на «Финмодель_Два_чайника_v1.5.xlsx». Ставки
-- берутся на дату: у заказа — на дату оплаты, у модели — на сегодня, так
-- что смена вводной не переписывает прошлые заказы.
--
-- Факт читается только из представлений analytics (без персональных
-- данных и тестовых заказов) и finance (выписка банка; таблицы cashflow
-- не меняются). «Нет данных» — NULL, а не 0.

-- Три параметра, которых нет в seed, но без которых формулы v1.5 не
-- считаются; значения — те же, что в таблице и в настройках бота.
insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select * from (values
    ('fiscal_units', 'Оплачиваемых единиц тарифа фискализации', 1::numeric, null, 'шт.', 'Из v1.5',
     'Лист 1, строка 61: отдельной платы за чек при выдаче нет', '2026-10-20'::date, 'импорт v1.5'),
    ('lead_product', 'Lead-продукт для безубыточности', null, 'Набор «Точка входа»', 'продукт', 'Из v1.5',
     'Лист 5, строка 7', '2026-10-20'::date, 'импорт v1.5'),
    ('delivery_acquiring_markup', 'Надбавка бота к цене доставки', 0.035, null, '%', 'Настройка бота',
     'DELIVERY_ACQUIRING_PERCENT=3.5: carrier_delivery_cost в analytics уже с надбавкой, перевозчику идёт цена без неё',
     '2026-10-20'::date, 'импорт v1.5')
) as v (key, label, value, value_text, unit, status, note, valid_from, entered_by)
where not exists (select 1 from fin.inputs i where i.key = v.key);

-- Числа для карточек Metabase: рубли без копеек, проценты с одним знаком,
-- пустое — «нет данных», а не 0 и не пустая клетка.
create or replace function fin.fmt_rub(x numeric) returns text
    language sql immutable as $$
    select case when x is null then 'нет данных'
                else replace(to_char(round(x), 'FM999G999G999G990'), ',', ' ') || ' ₽' end
$$;

create or replace function fin.fmt_pct(x numeric) returns text
    language sql immutable as $$
    select case when x is null then 'нет данных' else to_char(x * 100, 'FM9990.0') || ' %' end
$$;

-- Лист 1, строка 22: эквайринг × (1 + НДС на комиссию) + фискализация × единиц.
create or replace function fin.payment_rate(on_date date default current_date) returns numeric
    language sql stable as $$
    select fin.inp('acquiring_rate', on_date) * (1 + coalesce(fin.inp('vat_on_fee', on_date), 0))
         + coalesce(fin.inp('fiscal_rate', on_date), 0) * coalesce(fin.inp('fiscal_units', on_date), 1)
$$;

-- Лист 1, строки 63–64: активные конверсия консультаций и подрядчик по сценарию 1–4.
create or replace function fin.api_per_new_buyer(on_date date default current_date) returns numeric
    language sql stable as $$
    select fin.inp('api_usd_per_consult', on_date) * fin.inp('usd_rub', on_date)
           / nullif(case when fin.inp('scenario', on_date) in (1, 3) then fin.inp('api_conv_min', on_date)
                         else fin.inp('api_conv_risk', on_date) end, 0)
$$;

create or replace function fin.contractor_monthly(on_date date default current_date) returns numeric
    language sql stable as $$
    select case when fin.inp('scenario', on_date) in (1, 2) then fin.inp('contractor_paid_monthly', on_date) else 0 end
$$;

-- Месяц пилота n: М1 начинается pilot_start_date (лист 6, строка 30: EDATE).
create or replace function fin.pilot_month_start(n integer, on_date date default current_date) returns date
    language sql stable as $$
    select (fin.inp_date('pilot_start_date', on_date) + make_interval(months => n - 1))::date
$$;

-- Лист 6, строки 32–36: постоянные расходы месяца — сервисы + банк по
-- календарю + взносы соответствующего года / 12.
create or replace function fin.fixed_month(month_start date, on_date date default current_date) returns numeric
    language sql stable as $$
    select fin.inp('infra_monthly', on_date)
         + case when month_start >= fin.inp_date('bank_paid_from', on_date) then coalesce(fin.inp('bank_fee_monthly', on_date), 0) else 0 end
         + case extract(year from month_start)
               when 2026 then fin.inp('ip_fixed_contrib_2026', on_date)
               when 2027 then fin.inp('ip_fixed_contrib_2027', on_date)
           end / 12
$$;

-- Себестоимость грамма чая и входящей логистики. Лист 2, столбцы F, M:
-- пока партий нет — базовый прайс (fin.sku) и 144 ₽/кг из вводных; есть
-- партии — средневзвешенная по ним цена со скидкой и их доставка.
create or replace view fin.v_tea_cost as
select s.sku_name,
       s.price_per_unit_base / (s.unit_weight_kg * 1000) as base_rub_per_g,
       l.lot_rub_per_g,
       coalesce(l.lot_rub_per_g, s.price_per_unit_base / (s.unit_weight_kg * 1000)) as tea_rub_per_g,
       coalesce(l.lot_inbound_rub_per_g, fin.inp('inbound_logistics_per_kg') / 1000) as inbound_rub_per_g,
       case when l.lot_rub_per_g is null then 'прайс v1.5' else 'партии закупки' end as cost_source,
       l.lots, l.grams_bought
from fin.sku s
left join lateral (
    select count(*) as lots,
           sum(p.qty_units * s.unit_weight_kg * 1000) as grams_bought,
           sum(p.qty_units * p.price_per_unit * (1 - p.discount)) / nullif(sum(p.qty_units * s.unit_weight_kg * 1000), 0) as lot_rub_per_g,
           case when sum(p.delivery_cost) > 0
                then sum(p.delivery_cost) / nullif(sum(p.qty_units * s.unit_weight_kg * 1000), 0) end as lot_inbound_rub_per_g
    from fin.purchase_lots p where p.sku_name = s.sku_name
    having count(*) > 0
) l on true;
comment on view fin.v_tea_cost is
    'Цена грамма чая и входящей логистики по SKU: партии закупки, а пока их нет — прайс v1.5. Лист 2, столбцы F/M.';

-- Себестоимость и маржа единицы на дату. Пачки и блины — листы 2 (Q) и 3
-- (D–N), сеты и наборы — лист 4 (D–U).
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
           s.sale_pack_g as grams, s.retail_price,
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
           t.cost_source, s.bot_name, s.gtin
    from fin.sku s join fin.v_tea_cost t using (sku_name) cross join r
    where s.active
),
sets as (
    -- Лист 4: E, F — чай и логистика состава; G×H — пакеты; I — коробка
    -- (= прочие переменные на заказ, строка 29 вводных); J — коды, K — этикетки,
    -- L — Claude API, W — комиссия с доставки.
    select b.bundle as product,
           case when b.pouch_type = '35g' then 'набор' else 'сет' end as kind,
           sum(i.grams) as grams, b.retail_price,
           sum(i.grams * t.tea_rub_per_g / (1 - r.loss)) as tea_cost,
           sum(i.grams * t.inbound_rub_per_g / (1 - r.loss)) as inbound_cost,
           b.pouches,
           b.pouches * case when b.pouch_type = '35g' then r.pouch_35 else r.pouch_100 end as pouch_cost,
           b.codes_total as codes, b.codes_total * r.code_price as codes_cost,
           b.labels_total as labels, b.labels_total * r.label_unit as labels_cost,
           r.other_var as box_cost,
           r.api + r.subsidy + r.delivery_fees as order_extra_cost,
           min(t.cost_source) as cost_source, b.bot_name, b.gtin
    from fin.bundles b
    join fin.bundle_items i on i.bundle = b.bundle
    join fin.v_tea_cost t on t.sku_name = i.sku_name
    cross join r
    where b.active
    group by b.bundle, b.retail_price, b.pouches, b.pouch_type, b.codes_total, b.labels_total, b.bot_name, b.gtin,
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
comment on function fin.unit_costs(date) is 'Себестоимость и маржа единицы на дату. Листы 2, 3, 4.';

create or replace view fin.v_unit_cost as select * from fin.unit_costs(current_date);
comment on view fin.v_unit_cost is
    'Себестоимость и маржа до CAC пачек, блинов, сетов и наборов по сегодняшним вводным. Листы 2 (Q), 3 (D–N), 4 (M, R).';
comment on column fin.v_unit_cost.product_cost is
    'Себестоимость единицы: чай с потерями, входящая логистика, пакеты, коды, этикетки, у сета и набора — коробка. Лист 2 Q / лист 4 E+F+G·H+I+J+K.';
comment on column fin.v_unit_cost.full_cost is 'С расходами заказа и Claude API на нового покупателя. Лист 3 F / лист 4 M.';
comment on column fin.v_unit_cost.margin_before_cac is 'Маржа до CAC. Лист 3 L / лист 4 R.';

-- Позиции оплаченных заказов бота, связанные со справочником. Позиция
-- «Те Гуань Инь 100 г» ищется по bot_name и граммам, сет или набор — по
-- bot_name или названию целиком; gtin — когда появится в позициях.
create or replace view fin.v_order_lines as
with lines as (
    select i.order_id, i.line_no, i.item_name, i.product, i.quantity, i.price, i.amount,
           nullif(regexp_replace(coalesce(i.pack, ''), '[^0-9.,]', '', 'g'), '')::numeric as pack_g,
           lower(replace(i.product, 'ё', 'е')) as product_key,
           lower(replace(i.item_name, 'ё', 'е')) as item_key,
           o.paid_date_msk, o.is_refunded
    from analytics.v_order_items i
    join analytics.v_orders o on o.order_id = i.order_id
    where o.is_paid
)
select l.order_id, l.line_no, l.item_name, l.quantity, l.price, l.amount, l.paid_date_msk, l.is_refunded,
       m.product as fin_product, m.kind,
       m.tea_cost + m.inbound_cost as tea_unit_cost,
       m.pouch_cost + m.codes_cost + m.labels_cost + m.box_cost as packaging_unit_cost,
       m.product_cost as unit_cost,
       l.quantity * m.product_cost as line_cost,
       m.product is not null as has_cost
from lines l
left join lateral (
    select c.* from fin.unit_costs(l.paid_date_msk) c
    where (c.kind in ('пачка', 'блин')
           and lower(replace(coalesce(c.bot_name, c.product), 'ё', 'е')) = l.product_key
           and (l.pack_g is null or c.grams = l.pack_g))
       or (c.kind in ('сет', 'набор')
           and lower(replace(coalesce(c.bot_name, c.product), 'ё', 'е')) in (l.item_key, l.product_key))
    order by (c.kind in ('сет', 'набор')) desc
    limit 1
) m on true;
comment on view fin.v_order_lines is
    'Позиции оплаченных заказов с себестоимостью на дату оплаты. has_cost = false — позиция не связана со справочником fin.sku / fin.bundles.';

-- Экономика оплаченного заказа. Лист 3 (заказ из одной пачки) и лист 4 —
-- но по фактическим позициям, доставке и цене перевозчика.
create or replace view fin.v_order_economics as
with o as (
    select v.order_id, v.client_key, v.paid_date_msk, v.is_refunded, v.free_delivery,
           v.client_paid_order_number = 1 as is_first_order,
           v.total, v.items_total, coalesce(v.delivery_cost, 0) as delivery_paid_by_client,
           v.carrier_delivery_cost
    from analytics.v_orders v where v.is_paid
),
l as (
    select order_id,
           -- Хоть одна позиция без себестоимости — себестоимость заказа неизвестна
           -- целиком (NULL), а не «сумма известных»: иначе маржа вышла бы завышенной.
           case when bool_and(has_cost) then sum(quantity * tea_unit_cost) end as cogs_tea,
           case when bool_and(has_cost) then sum(quantity * packaging_unit_cost) end as packaging_items,
           count(*) filter (where not has_cost) as lines_without_cost,
           bool_or(kind in ('сет', 'набор')) as has_bundle,
           coalesce(sum(quantity) filter (where kind in ('пачка', 'блин')), 0) as packs,
           sum(quantity) as units
    from fin.v_order_lines group by order_id
),
e as (
    select o.order_id, o.client_key, o.paid_date_msk, o.is_first_order, o.is_refunded, o.free_delivery,
           o.total as revenue, o.items_total, o.delivery_paid_by_client,
           l.cogs_tea,
           -- Коробка и этикетка отправки на заказ; у сета и набора коробка уже в
           -- его себестоимости (лист 4, I и K), второй раз не считаем.
           l.packaging_items + case when coalesce(l.has_bundle, false) then 0
                                    else fin.inp('other_variable_per_order', o.paid_date_msk)
                                         + fin.inp('shipping_label_cost', o.paid_date_msk) end as packaging_and_codes,
           -- Цена перевозчика: в analytics она с надбавкой бота на эквайринг, перевозчику идёт без неё.
           o.carrier_delivery_cost / (1 + coalesce(fin.inp('delivery_acquiring_markup', o.paid_date_msk), 0)) as carrier_cost,
           o.total * fin.payment_rate(o.paid_date_msk) as acquiring,
           o.total * fin.inp('usn_rate', o.paid_date_msk) as usn_reserve,
           -- Лист 1, строки 51 и 72: API на нового покупателя; у повторного факта нет — 0.
           case when o.is_first_order then fin.api_per_new_buyer(o.paid_date_msk)
                else coalesce(fin.inp('api_repeat_order', o.paid_date_msk), 0) end as api_cost,
           coalesce(l.lines_without_cost, 0) as lines_without_cost, coalesce(l.has_bundle, false) as has_bundle,
           l.packs, l.units, coalesce(l.packs, 0) >= 2 as two_plus_packs
    from o left join l using (order_id)
)
select e.*,
       -- Субсидия доставки: сколько перевозчику заплатили сверх того, что заплатил клиент.
       coalesce(e.carrier_cost, 0) - e.delivery_paid_by_client as delivery_subsidy,
       e.revenue - e.cogs_tea - e.packaging_and_codes - coalesce(e.carrier_cost, 0)
           - e.acquiring - e.usn_reserve - e.api_cost as margin_before_cac,
       (e.revenue - e.cogs_tea - e.packaging_and_codes - coalesce(e.carrier_cost, 0)
           - e.acquiring - e.usn_reserve - e.api_cost) / nullif(e.revenue, 0) as margin_pct,
       case when e.lines_without_cost > 0 then 'не все позиции связаны со справочником' end as warning
from e;
comment on view fin.v_order_economics is
    'Экономика оплаченного заказа: выручка с доставкой, себестоимость по позициям, упаковка, перевозчик, эквайринг, резерв УСН, Claude API, маржа до CAC, первый ли заказ клиента. Листы 3–4 на факте.';
comment on column fin.v_order_economics.margin_before_cac is
    'Выручка − чай − упаковка и коды − перевозчик − эквайринг − резерв УСН − Claude API. NULL — есть позиции без себестоимости.';

-- Маржа до CAC по продуктам: план (сегодняшние вводные) против факта.
-- Маржа заказа делится между позициями пропорционально их сумме.
create or replace view fin.v_product_margin as
with fact as (
    select l.fin_product as product, sum(l.quantity) as units_sold, count(distinct l.order_id) as orders,
           sum(e.margin_before_cac * l.amount / nullif(e.items_total, 0)) / nullif(sum(l.quantity), 0) as fact_margin_per_unit
    from fin.v_order_lines l join fin.v_order_economics e using (order_id)
    where l.has_cost and e.lines_without_cost = 0
    group by l.fin_product
)
select u.product, u.kind, u.retail_price, u.product_cost, u.full_cost,
       u.margin_before_cac as plan_margin, u.margin_pct as plan_margin_pct,
       f.units_sold, f.orders, f.fact_margin_per_unit,
       f.fact_margin_per_unit - u.margin_before_cac as fact_minus_plan
from fin.v_unit_cost u left join fact f using (product);
comment on view fin.v_product_margin is 'Маржа до CAC по продуктам: плановая (fin.v_unit_cost) и фактическая на единицу по оплаченным заказам.';

-- Месяцы пилота: М0 — до старта (с регистрации ИП), М1…Мn от pilot_start_date.
create or replace view fin.v_pilot_months as
select n as month_no,
       case when n = 0 then 'М0 (до пилота)' else 'М' || n end as month_label,
       case when n = 0 then fin.inp_date('ip_registration_date') else fin.pilot_month_start(n) end as period_start,
       fin.pilot_month_start(n + 1) as period_end
from generate_series(0, greatest(coalesce(fin.inp('cash_horizon_months')::int, 6), 1)) as n;

-- Выписка по статьям за день, без контрагентов и назначений — только суммы.
create or replace view fin.v_bank_daily as
select f.operation_date as day, f.article_id, a.grp, a.section, a.is_capex,
       sum(f.amount) filter (where f.direction = 'in') as inflow,
       sum(f.amount) filter (where f.direction = 'out') as outflow
from finance.fact_operations f
join finance.articles a using (article_id)
group by 1, 2, 3, 4, 5;
comment on view fin.v_bank_daily is 'Выписка банка по дням и статьям cashflow — суммы без контрагентов.';

-- P&L по месяцам пилота. Лист 8, строки 4–22.
create or replace view fin.v_pnl_base as
with m as (select * from fin.v_pilot_months),
orders as (
    select m.month_no, count(*) as orders, count(*) filter (where e.is_first_order) as new_clients,
           sum(e.revenue) as revenue, sum(e.cogs_tea) as cogs_tea, sum(e.packaging_and_codes) as packaging_and_codes,
           sum(e.delivery_subsidy) as delivery_subsidy, sum(e.revenue) filter (where e.is_refunded) as returns,
           sum(e.acquiring) as acquiring,
           count(*) filter (where e.is_first_order) as first_orders,
           sum(e.margin_before_cac + e.usn_reserve) filter (where e.is_first_order) as first_orders_margin_before_usn
    from fin.v_order_economics e
    join m on e.paid_date_msk >= m.period_start and e.paid_date_msk < m.period_end
    group by m.month_no
),
ads as (
    select m.month_no, sum(a.spend_own) as ads, sum(a.spend_bonus) as ads_bonus
    from fin.ad_spend a join m on a.date >= m.period_start and a.date < m.period_end group by m.month_no
),
bank as (
    -- Подрядчик и прочие операционные — по выписке. Закупка, упаковка,
    -- коды и доставка — уже в себестоимости заказов; оборудование — вложения;
    -- пополнение кабинета ВК — деньги, а расход рекламы — fin.ad_spend.
    select m.month_no,
           sum(b.outflow) filter (where b.article_id = 'cfo_out_ads_contractor') as contractor,
           sum(b.outflow) filter (where b.article_id in ('cfo_out_services', 'cfo_out_bank_fees', 'cfo_out_other')) as other_opex
    from fin.v_bank_daily b join m on b.day >= m.period_start and b.day < m.period_end group by m.month_no
),
manual as (
    select m.month_no,
           sum(x.amount) filter (where x.category = 'подрядчик') as contractor,
           sum(x.amount) filter (where x.category in ('сервисы', 'банк', 'прочее')) as other_opex
    from fin.expenses x join m on x.date >= m.period_start and x.date < m.period_end
    where not x.is_plan and x.paid_from = 'мимо счёта' group by m.month_no
),
api as (
    -- Claude API по счёту Anthropic: в выписке ИП его нет, платится картой мимо счёта.
    select m.month_no, sum(c.amount_usd) * fin.inp('usd_rub') as claude_api
    from analytics.v_anthropic_costs c join m on c.day >= m.period_start and c.day < m.period_end group by m.month_no
),
p as (
    select m.month_no, m.month_label, m.period_start, m.period_end, m.period_start <= current_date as started,
           o.orders, o.new_clients, o.revenue, o.cogs_tea, o.packaging_and_codes, o.delivery_subsidy, o.returns,
           o.acquiring, o.first_orders, o.first_orders_margin_before_usn,
           a.ads, a.ads_bonus,
           coalesce(b.contractor, 0) + coalesce(x.contractor, 0) as contractor,
           coalesce(b.other_opex, 0) + coalesce(x.other_opex, 0) + coalesce(c.claude_api, 0) as other_opex,
           c.claude_api
    from m left join orders o using (month_no) left join ads a using (month_no) left join bank b using (month_no)
    left join manual x using (month_no) left join api c using (month_no)
)
select p.month_no, p.month_label, p.period_start, p.period_end,
       -- Месяц ещё не начался — факта нет (NULL); начался без продаж — честные нули.
       case when p.started then coalesce(p.orders, 0) end as orders,
       case when p.started then coalesce(p.new_clients, 0) end as new_clients,
       case when p.started then coalesce(p.revenue, 0) end as revenue,
       case when p.started then coalesce(p.cogs_tea, 0) end as cogs_tea,
       case when p.started then coalesce(p.packaging_and_codes, 0) end as packaging_and_codes,
       case when p.started then coalesce(p.delivery_subsidy, 0) end as delivery_subsidy,
       case when p.started then coalesce(p.returns, 0) end as returns,
       case when p.started then coalesce(p.acquiring, 0) end as acquiring,
       -- Строка 12: маржинальный доход до CAC.
       case when p.started then coalesce(p.revenue, 0) - coalesce(p.cogs_tea, 0) - coalesce(p.packaging_and_codes, 0)
           - coalesce(p.delivery_subsidy, 0) - coalesce(p.returns, 0) - coalesce(p.acquiring, 0) end as margin_before_cac,
       case when p.started then coalesce(p.ads, 0) end as ads,
       case when p.started then coalesce(p.ads_bonus, 0) end as ads_bonus,
       case when p.started then p.contractor end as contractor,
       case when p.started then p.other_opex end as other_opex,
       case when p.started then p.claude_api end as claude_api,
       -- Строки 16–17: резерв УСН (подтверждённого уменьшения на взносы пока нет) и взносы года / 12.
       case when p.started then coalesce(p.revenue, 0) * fin.inp('usn_rate') end as usn_reserve,
       case when p.started and p.month_no > 0 then
           case extract(year from p.period_start) when 2026 then fin.inp('ip_fixed_contrib_2026')
                                                  when 2027 then fin.inp('ip_fixed_contrib_2027') end / 12 end as contributions_reserve,
       -- Строка 18: операционная прибыль.
       case when p.started then
           coalesce(p.revenue, 0) - coalesce(p.cogs_tea, 0) - coalesce(p.packaging_and_codes, 0) - coalesce(p.delivery_subsidy, 0)
           - coalesce(p.returns, 0) - coalesce(p.acquiring, 0) - coalesce(p.ads, 0) - p.contractor - p.other_opex
           - coalesce(p.revenue, 0) * fin.inp('usn_rate')
           - case when p.month_no > 0 then
                 case extract(year from p.period_start) when 2026 then fin.inp('ip_fixed_contrib_2026')
                                                        when 2027 then fin.inp('ip_fixed_contrib_2027') end / 12
             else 0 end
       end as operating_profit,
       -- Строки 20–22: средний чек, полный CAC, первый заказ после CAC.
       p.revenue / nullif(p.orders, 0) as avg_check,
       (coalesce(p.ads, 0) + p.contractor) / nullif(p.new_clients, 0) as cac,
       p.first_orders_margin_before_usn / nullif(p.first_orders, 0)
           - (coalesce(p.ads, 0) + p.contractor) / nullif(p.new_clients, 0) as first_order_after_cac
from p;
comment on view fin.v_pnl_base is
    'Строки P&L по месяцам пилота, лист 8. Будущие месяцы — NULL, начавшиеся без продаж — нули. Резерв УСН без зачёта взносов.';

-- P&L для дашборда: базовые строки плюс операционная маржа и накопленный
-- результат пилота — отдельным шагом, чтобы не повторять формулу прибыли.
create or replace view fin.v_pnl_monthly as
select p.*, p.operating_profit / nullif(p.revenue, 0) as operating_margin,
       sum(p.operating_profit) filter (where p.month_no between 1 and coalesce(fin.inp('pilot_months')::int, 3))
           over (order by p.month_no) as pilot_cumulative_profit
from fin.v_pnl_base p;
comment on view fin.v_pnl_monthly is
    'P&L по месяцам пилота (М1 — с pilot_start_date), лист 8: выручка, себестоимость, упаковка и коды, субсидия доставки, возвраты, эквайринг, маржа, реклама, подрядчик, прочие, резерв УСН и взносов, операционная прибыль.';

-- Бюджет пилота: известные расходы против плана капитала. Лист 6, строки 4–26.
create or replace view fin.v_pilot_budget as
with purchase as (
    select sum(price_per_unit_base * qty_units_first_purchase) as gross,
           sum(unit_weight_kg * qty_units_first_purchase) as kg
    from fin.sku where active
),
bank_fees as (
    -- Строка 24: обслуживание банка по календарю месяцев пилота (строка 32).
    select sum(case when fin.pilot_month_start(n) >= fin.inp_date('bank_paid_from') then fin.inp('bank_fee_monthly') else 0 end) as total
    from generate_series(1, coalesce(fin.inp('pilot_months')::int, 3)) n
),
contrib as (
    -- Лист 1, строка 65: взносы 2026 с даты регистрации ИП, неполный первый месяц — по дням.
    select round(fin.inp('ip_fixed_contrib_2026') / 12 * (
               12 - extract(month from d)
               + (extract(day from (date_trunc('month', d) + interval '1 month - 1 day')) - extract(day from d) + 1)
                 / extract(day from (date_trunc('month', d) + interval '1 month - 1 day'))), 2) as total
    from (select fin.inp_date('ip_registration_date') as d) x
),
lines (line_no, item, plan_amount, max_amount, source) as (
    select 1, 'Первая закупка чая', p.gross * (1 - coalesce(fin.inp('first_order_discount'), 0)), p.gross, 'лист 6, строка 5' from purchase p
    union all select 2, 'Упаковка и расходники (лимит)', fin.inp('packaging_limit'), fin.inp('packaging_limit'), 'лист 6, строка 6'
    union all select 3, 'Термопринтер', fin.inp('printer_cost'), fin.inp('printer_cost'), 'лист 6, строка 7'
    union all select 4, 'ВК: пополнение кабинета', fin.inp('vk_topup_own'), fin.inp('vk_topup_own'), 'лист 6, строка 8'
    union all select 5, 'Подрядчик за пилот', fin.contractor_monthly() * fin.inp('contractor_months'),
                        fin.contractor_monthly() * fin.inp('contractor_months'), 'лист 6, строка 9'
    union all select 6, 'Доставка основной партии', p.kg * fin.inp('inbound_logistics_per_kg'),
                        p.kg * fin.inp('inbound_logistics_per_kg'), 'лист 6, строка 11' from purchase p
    union all select 7, 'Весы', fin.inp('scales_cost'), fin.inp('scales_cost'), 'лист 6, строка 12'
    union all select 8, 'ККТ, ФН, фискализация', fin.inp('kkt_cost'), fin.inp('kkt_cost'), 'лист 6, строка 13'
    union all select 9, 'УКЭП / ЭДО / маркировка', fin.inp('edo_ukep_cost'), fin.inp('edo_ukep_cost'), 'лист 6, строка 14'
    union all select 10, 'GTIN / карточки товаров', fin.inp('gtin_cost'), fin.inp('gtin_cost'), 'лист 6, строка 15'
    union all select 11, 'Сервисы за пилот', fin.inp('infra_monthly') * fin.inp('pilot_months'),
                         fin.inp('infra_monthly') * fin.inp('pilot_months'), 'лист 6, строка 17'
    union all select 12, 'Взносы 2026: расчётный резерв', c.total, c.total, 'лист 6, строка 18' from contrib c
    union all select 13, 'Обслуживание банка за пилот', b.total, b.total, 'лист 6, строка 24' from bank_fees b
    union all select 14, 'API тестов и консультаций до продаж', null, null, 'лист 6, строка 25: нет биллинга'
    union all select 15, 'Тестовые отправки до рекламы', fin.inp('test_shipments') * fin.inp('free_delivery_cost'),
                         fin.inp('test_shipments') * fin.inp('free_delivery_cost'), 'лист 6, строка 26'
)
select line_no, item, plan_amount, max_amount, source,
       sum(plan_amount) over () as known_total,
       fin.inp('capital_plan') as capital_plan,
       fin.inp('capital_plan') - sum(plan_amount) over () as left_before_unknown
from lines;
comment on view fin.v_pilot_budget is
    'Известные расходы пилота против стартового капитала. Лист 6, строки 4–26, итог — строка 19. Пустая статья — «нет данных», в сумму не входит.';

-- Безубыточность месяца: заказов lead-продукта, чтобы покрыть привлечение и
-- постоянные расходы. Лист 5, строки 35–37 (М1); следующие месяцы — с
-- рекламой из плана (fin.plan, metric = 'ads').
create or replace view fin.v_breakeven as
with lead as (
    select u.product, u.margin_before_cac from fin.v_unit_cost u
    where u.product = (select value_text from fin.inputs_current where key = 'lead_product')
),
plan_ads as (
    select distinct on (month) month, value from fin.plan where metric = 'ads' order by month, created_at desc
)
select m.month_no, m.month_label, m.period_start,
       l.product as lead_product, l.margin_before_cac as lead_margin,
       fin.fixed_month(m.period_start) as fixed_costs,
       coalesce(pa.value, case when m.month_no = 1 then fin.inp('first_ad_test_own') end) as acquisition_ads,
       fin.contractor_monthly() as contractor,
       case when l.margin_before_cac > 0 and coalesce(pa.value, case when m.month_no = 1 then fin.inp('first_ad_test_own') end) is not null
            then ceil((coalesce(pa.value, case when m.month_no = 1 then fin.inp('first_ad_test_own') end)
                       + fin.contractor_monthly() + fin.fixed_month(m.period_start)) / l.margin_before_cac) end as orders_to_break_even
from fin.v_pilot_months m
cross join lead l
left join plan_ads pa on pa.month = m.month_no
where m.month_no >= 1;
comment on view fin.v_breakeven is
    'Заказов lead-продукта для безубыточности месяца = (реклама + подрядчик + постоянные) / маржа до CAC. Лист 5, строка 37; М2+ — с рекламой из fin.plan.';

-- CAC по неделям и месяцам (календарным): реклама своими и бонусами,
-- подрядчик, новые клиенты, первый заказ после CAC; сценарные оси — из вводных.
create or replace view fin.v_cac as
with periods as (
    select 'неделя'::text as period_type, date_trunc('week', d)::date as period_start
    from generate_series(fin.inp_date('ip_registration_date'), current_date, interval '1 day') d
    union
    select 'месяц', date_trunc('month', d)::date
    from generate_series(fin.inp_date('ip_registration_date'), current_date, interval '1 day') d
),
p as (
    select period_type, period_start,
           (period_start + case period_type when 'неделя' then interval '1 week' else interval '1 month' end)::date as period_end
    from periods
)
select p.period_type, p.period_start,
       a.spend_own, a.spend_bonus, a.impressions, a.clicks,
       coalesce(b.contractor, 0) + coalesce(x.contractor, 0) as contractor,
       f.new_clients, f.first_order_margin,
       (coalesce(a.spend_own, 0) + coalesce(b.contractor, 0) + coalesce(x.contractor, 0)) / nullif(f.new_clients, 0) as cac,
       f.first_order_margin - (coalesce(a.spend_own, 0) + coalesce(b.contractor, 0) + coalesce(x.contractor, 0)) / nullif(f.new_clients, 0)
           as first_order_after_cac,
       f.first_order_margin - fin.inp('cac_low') as after_cac_low,
       f.first_order_margin - fin.inp('cac_mid') as after_cac_mid,
       f.first_order_margin - fin.inp('cac_high') as after_cac_high,
       fe.dialogs_started, fe.drafts, fe.invoices, f.new_clients as first_paid
from p
left join lateral (
    select sum(spend_own) as spend_own, sum(spend_bonus) as spend_bonus, sum(impressions) as impressions, sum(clicks) as clicks
    from fin.ad_spend where date >= p.period_start and date < p.period_end
) a on true
left join lateral (
    select sum(outflow) as contractor from fin.v_bank_daily
    where article_id = 'cfo_out_ads_contractor' and day >= p.period_start and day < p.period_end
) b on true
left join lateral (
    select sum(amount) as contractor from fin.expenses
    where category = 'подрядчик' and not is_plan and paid_from = 'мимо счёта' and date >= p.period_start and date < p.period_end
) x on true
left join lateral (
    -- Маржа первого заказа до CAC и до резерва УСН — как строка 32 листа 8.
    select count(*) as new_clients, avg(margin_before_cac + usn_reserve) as first_order_margin
    from fin.v_order_economics where is_first_order and paid_date_msk >= p.period_start and paid_date_msk < p.period_end
    having count(*) > 0
) f on true
left join lateral (
    -- Воронка бота: запуск диалога, черновик, счёт. Консультации отдельным событием нет.
    select count(*) filter (where event = 'dialog_start') as dialogs_started,
           count(*) filter (where event = 'draft_created') as drafts,
           count(*) filter (where event like 'invoice\_%' and event <> 'invoice_expired') as invoices
    from analytics.v_funnel_events where created_date_msk >= p.period_start and created_date_msk < p.period_end
) fe on true;
comment on view fin.v_cac is
    'CAC по неделям и месяцам: реклама (свои и бонусы), подрядчик, новые клиенты, первый заказ после CAC, сценарные оси 300/500/700, воронка бота. Листы 5 и 8, строки 21–22.';

-- Склад: граммы по SKU, пачки, сеты и наборы. Пачки на руках =
-- расфасовано − продано − возвращено (возвращённый заказ считаем ушедшим:
-- вернулся ли чай на полку, база не знает). Сигнал докупки — пачек меньше
-- чем на 14 дней по средней продаже за 14 дней.
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
    -- Граммы, ушедшие в сеты и наборы: состав × число собранных.
    select i.sku_name, sum(i.grams * r.packs_out) as grams
    from fin.packing_runs r join fin.bundle_items i on i.bundle = r.product group by i.sku_name
),
products as (
    select sku_name as product, case when supplier_unit ilike 'пресс%' then 'блин' else 'пачка' end as kind from fin.sku where active
    union all
    select bundle, case when pouch_type = '35g' then 'набор' else 'сет' end from fin.bundles where active
)
select p.product, p.kind,
       t.grams_bought,
       case when p.kind in ('пачка', 'блин') then coalesce(pk.grams_in, 0) + coalesce(bg.grams, 0) end as grams_used,
       case when p.kind in ('пачка', 'блин') then t.grams_bought - coalesce(pk.grams_in, 0) - coalesce(bg.grams, 0) end as grams_left,
       pk.packed, s.sold, s.refunded,
       pk.packed - coalesce(s.sold, 0) - coalesce(s.refunded, 0) as on_hand,
       s.sold_14d / 14.0 as avg_daily_sales_14d,
       (pk.packed - coalesce(s.sold, 0) - coalesce(s.refunded, 0)) / nullif(s.sold_14d / 14.0, 0) as days_left,
       coalesce((pk.packed - coalesce(s.sold, 0) - coalesce(s.refunded, 0)) / nullif(s.sold_14d / 14.0, 0) < 14, false) as reorder
from products p
left join fin.v_tea_cost t on t.sku_name = p.product
left join packed pk on pk.product = p.product
left join bundle_grams bg on bg.sku_name = p.product
left join sold s on s.product = p.product;
comment on view fin.v_stock is
    'Склад: граммы чая (закуплено − расфасовано − ушло в сеты), пачки и сеты на руках, сигнал «пора докупать» (меньше 14 дней продаж). Пусто — нет данных.';

-- Коды «Честного знака» по GTIN: из пула СУЗ, в заказах, свободно.
-- «Напечатано» база бота не знает — печать не отмечается.
create or replace view fin.v_codes as
select gtin,
       count(*) filter (where from_pool) as imported,
       count(*) filter (where status in ('assigned', 'sold')) as used_in_orders,
       count(*) filter (where status = 'in_stock') as available,
       count(*) filter (where status = 'returned') as returned,
       null::bigint as printed
from public.marking_codes
group by gtin;
comment on view fin.v_codes is 'Коды ЧЗ по GTIN: импортировано из СУЗ, в заказах, свободно, вернулось. Печать не отмечается — printed пусто.';

-- Деньги по дням — по выписке банка. Лист 7 (банковский факт).
create or replace view fin.v_cash as
select b.day, b.balance,
       d.inflow, d.outflow, d.customers_in, d.owners_in, d.transfers_out, d.ads_out, d.goods_out, d.capex_out, d.other_out,
       s.balance as snapshot_balance, s.balance - b.balance as snapshot_diff
from finance.v_daily_balance_total b
left join lateral (
    -- Всё движение по счёту, включая переводы на свои счета: остаток меняется на
    -- inflow − outflow. Переводы — отдельно: так оплачивают покупки с карты
    -- (20.10.2026 — упаковка 3 699 ₽), сама покупка — в fin.expenses «мимо счёта».
    select sum(inflow) as inflow, sum(outflow) as outflow,
           sum(inflow) filter (where grp = 'Выручка') as customers_in,
           sum(inflow) filter (where article_id = 'cff_owner_contribution') as owners_in,
           sum(outflow) filter (where article_id = 'tech_internal_transfer') as transfers_out,
           sum(outflow) filter (where grp = 'Маркетинг') as ads_out,
           sum(outflow) filter (where grp = 'Товар') as goods_out,
           sum(outflow) filter (where is_capex) as capex_out,
           sum(outflow) filter (where grp not in ('Маркетинг', 'Товар') and not is_capex
                                  and article_id <> 'tech_internal_transfer') as other_out
    from fin.v_bank_daily where day = b.day
) d on true
left join lateral (
    select balance from fin.cash_snapshots where date = b.day order by created_at desc limit 1
) s on true;
comment on view fin.v_cash is 'Остаток и движение денег по дням из выписки (finance), переводы на свои счета — отдельно; сверка со снимками fin.cash_snapshots.';

-- Сводка «Деньги»: остаток, сверка, бюджет пилота, лимит убытка. Лист 12, строки 76–80.
create or replace view fin.v_cash_status as
with last_day as (select max(day) as day from finance.v_daily_balance_total),
pnl as (
    select count(*) filter (where operating_profit is not null) as months_with_fact,
           sum(operating_profit) as profit
    from fin.v_pnl_base where month_no between 1 and coalesce(fin.inp('pilot_months')::int, 3)
),
transit as (
    -- Оплачено покупателями, но ещё не пришло от ЮKassa: после последнего дня выписки.
    select sum(e.revenue - e.acquiring) as amount
    from fin.v_order_economics e cross join last_day l where e.paid_date_msk > l.day
),
snap as (select date, balance from fin.cash_snapshots order by date desc, created_at desc limit 1),
holds as (
    -- Холды банка: остаток уже уменьшился, а операций в выписке ещё нет.
    select sum(holds_out) as holds_out, sum(holds_in) as holds_in, max(checked_at) as checked_at
    from finance.v_pending_summary
)
select l.day as bank_date,
       (select balance from finance.v_daily_balance_total where day = l.day) as bank_balance,
       h.holds_out, h.holds_in, h.checked_at as holds_checked_at,
       t.amount as yookassa_in_transit,
       fin.inp('min_cash_reserve') as min_cash_reserve,
       sn.date as snapshot_date, sn.balance as snapshot_balance,
       sn.balance - (select balance from finance.v_daily_balance_total where day = sn.date) as snapshot_diff,
       (select max(known_total) from fin.v_pilot_budget) as pilot_known_costs,
       fin.inp('capital_plan') as capital_plan,
       (select sum(inflow) from fin.v_bank_daily where article_id = 'cff_owner_contribution') as owners_in_bank,
       (select sum(amount) filter (where kind = 'вклад') - coalesce(sum(amount) filter (where kind = 'выплата'), 0) from fin.capital)
           as partners_net_marked,
       case when p.months_with_fact = 0 then null else greatest(0, -p.profit) end as pilot_loss,
       fin.inp('pilot_pause_loss') as pilot_pause_loss,
       fin.inp('pilot_max_loss') as pilot_max_loss,
       case when p.months_with_fact = 0 then 'НЕТ ФАКТА'
            when greatest(0, -p.profit) >= fin.inp('pilot_max_loss') then 'СТОП НОВЫЕ РАСХОДЫ'
            when greatest(0, -p.profit) >= fin.inp('pilot_pause_loss') then 'ПАУЗА И РАЗБОР'
            else 'ЛИМИТ НЕ ДОСТИГНУТ' end as pilot_status
from last_day l cross join pnl p cross join transit t cross join holds h left join snap sn on true;
comment on view fin.v_cash_status is
    'Деньги сейчас: остаток по выписке, холды банка, ЮKassa в пути, сверка со снимком, известные расходы пилота, статус лимита убытка (лист 12, строки 76–80).';

-- Последние записи форм — для дашборда «Ввод данных».
create or replace view fin.v_recent_entries as
select * from (
    select 'Вводная' as form, created_at, entered_by, key || ' = ' || coalesce(value::text, value_text, '—') as what, note
    from fin.inputs where entered_by <> 'импорт v1.5'
    union all select 'Расход', created_at, entered_by, category || ': ' || amount || ' ₽ ' || coalesce(description, ''),
                     case when is_plan then 'план' else paid_from end from fin.expenses
    union all select 'Вклад / выплата', created_at, entered_by, partner || ' ' || kind || ' ' || amount || ' ₽', note from fin.capital
    union all select 'Остаток по выписке', created_at, entered_by, date || ': ' || balance || ' ₽', source from fin.cash_snapshots
    union all select 'Партия закупки', created_at, entered_by, sku_name || ': ' || qty_units || ' × ' || price_per_unit || ' ₽', supplier
              from fin.purchase_lots
    union all select 'Фасовка', created_at, entered_by, product || ': ' || packs_out || ' шт', note from fin.packing_runs
    union all select 'Реклама за день', created_at, entered_by, date || ' ' || channel || ': ' || spend_own || ' ₽ своих', note
              from fin.ad_spend
) e order by created_at desc limit 20;

insert into fin.schema_migrations (version) values ('003_views') on conflict do nothing;
