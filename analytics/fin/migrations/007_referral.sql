-- 007: рефералка (пакет D) — скидка по коду друга отдельной строкой экономики
-- заказа и CAC реферального канала.
--
-- Скидку бот вписывает в цены позиций (чек 54-ФЗ без строки «скидка»), поэтому
-- выручка заказа (total) уже за вычетом скидки; сама скидка — в
-- analytics.v_orders.referral_discount. Здесь она — отдельный столбец заказа и
-- «цена привлечения» нового клиента, пришедшего по коду друга.
-- Столбцы существующих представлений только дописываются в конец.

-- Экономика заказа: тот же расчёт, что в 006, плюс столбец скидки в конце.
create or replace view fin.v_order_economics as
with o as (
    select v.order_id, v.client_key, v.paid_date_msk, v.is_refunded, v.free_delivery,
           v.client_paid_order_number = 1 as is_first_order,
           v.total, v.items_total, coalesce(v.delivery_cost, 0) as delivery_paid_by_client,
           v.carrier_delivery_cost, v.payment_method, v.carrier_actual_cost, v.carrier_track,
           v.referral_discount
    from analytics.v_orders v where v.is_paid
),
l as (
    select order_id,
           case when bool_and(has_cost) then sum(quantity * tea_unit_cost) end as cogs_tea,
           case when bool_and(has_cost) then sum(quantity * packaging_unit_cost) end as packaging_items,
           count(*) filter (where not has_cost) as lines_without_cost,
           bool_or(kind in ('сет', 'набор')) as has_bundle,
           coalesce(sum(quantity) filter (where kind in ('пачка', 'блин')), 0) as packs,
           sum(quantity) as units
    from fin.v_order_lines group by order_id
),
fee as (
    select order_id, sum(fee) as fee, max(payment_method) as method
    from finance.yk_payments where status = 'succeeded' and order_id is not null and income_amount is not null
    group by order_id
),
acts as (
    select track,
           (array_agg(amount order by created_at desc) filter (where kind = 'доставка'))[1] as delivery,
           (array_agg(amount order by created_at desc) filter (where kind = 'возврат'))[1] as return_cost
    from fin.carrier_acts group by track
),
e as (
    select o.order_id, o.client_key, o.paid_date_msk, o.is_first_order, o.is_refunded, o.free_delivery,
           o.total as revenue, o.items_total, o.delivery_paid_by_client,
           l.cogs_tea,
           l.packaging_items + case when coalesce(l.has_bundle, false) then 0
                                    else fin.inp('other_variable_per_order', o.paid_date_msk)
                                         + fin.inp('shipping_label_cost', o.paid_date_msk) end as packaging_and_codes,
           o.carrier_delivery_cost / (1 + coalesce(fin.inp('delivery_acquiring_markup', o.paid_date_msk), 0)) as carrier_quote,
           coalesce(a.delivery, o.carrier_actual_cost) as carrier_actual,
           coalesce(a.return_cost, 0) as carrier_return_cost,
           case when a.delivery is not null then 'акт' when o.carrier_actual_cost is not null then 'API перевозчика'
                else 'котировка' end as carrier_cost_source,
           coalesce(f.method, o.payment_method) as payment_method,
           case when f.fee is not null
                then f.fee + o.total * coalesce(fin.inp('fiscal_rate', o.paid_date_msk), 0)
                                     * coalesce(fin.inp('fiscal_units', o.paid_date_msk), 1)
                else o.total * fin.payment_rate_for(coalesce(f.method, o.payment_method), o.paid_date_msk) end as acquiring,
           case when f.fee is not null then 'факт ЮKassa' else 'ставка способа' end as acquiring_source,
           o.total * fin.inp('usn_rate', o.paid_date_msk) as usn_reserve,
           case when o.is_first_order then fin.api_per_new_buyer(o.paid_date_msk)
                else coalesce(fin.inp('api_repeat_order', o.paid_date_msk), 0) end as api_cost,
           coalesce(l.lines_without_cost, 0) as lines_without_cost, coalesce(l.has_bundle, false) as has_bundle,
           l.packs, l.units, coalesce(l.packs, 0) >= 2 as two_plus_packs,
           o.referral_discount
    from o left join l using (order_id) left join fee f using (order_id) left join acts a on a.track = o.carrier_track
),
c as (
    select e.*, coalesce(e.carrier_actual, e.carrier_quote) as carrier_cost from e
)
select c.order_id, c.client_key, c.paid_date_msk, c.is_first_order, c.is_refunded, c.free_delivery,
       c.revenue, c.items_total, c.delivery_paid_by_client, c.cogs_tea, c.packaging_and_codes,
       c.carrier_cost, c.acquiring, c.usn_reserve, c.api_cost, c.lines_without_cost, c.has_bundle,
       c.packs, c.units, c.two_plus_packs,
       coalesce(c.carrier_cost, 0) - c.delivery_paid_by_client as delivery_subsidy,
       c.revenue - c.cogs_tea - c.packaging_and_codes - coalesce(c.carrier_cost, 0) - c.carrier_return_cost
           - c.acquiring - c.usn_reserve - c.api_cost as margin_before_cac,
       (c.revenue - c.cogs_tea - c.packaging_and_codes - coalesce(c.carrier_cost, 0) - c.carrier_return_cost
           - c.acquiring - c.usn_reserve - c.api_cost) / nullif(c.revenue, 0) as margin_pct,
       case when c.lines_without_cost > 0 then 'не все позиции связаны со справочником' end as warning,
       -- новые столбцы (006)
       c.carrier_quote, c.carrier_actual, c.carrier_return_cost, c.carrier_cost_source,
       c.payment_method, c.acquiring_source,
       -- 007: скидка по коду друга — уже вычтена из выручки (цены позиций), здесь отдельной строкой.
       c.referral_discount
from c;

create or replace view fin.v_order_discounts as
select v.order_id, v.client_key, v.paid_date_msk, v.referral_discount, v.referral_code_id, v.items_list_total,
       v.first_source_kind, v.client_paid_order_number = 1 as is_first_order
from analytics.v_orders v where v.is_paid;
comment on view fin.v_order_discounts is
    'Скидки по коду друга и наградам пригласившим по оплаченным заказам: сумма, код, прайс товаров до скидки.';

create or replace view fin.v_referral_cac as
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
       count(d.order_id) filter (where d.is_first_order and d.first_source_kind = 'referral') as new_clients_referral,
       coalesce(sum(d.referral_discount) filter (where d.is_first_order and d.first_source_kind = 'referral'), 0)
           as referral_discounts,
       sum(d.referral_discount) filter (where d.is_first_order and d.first_source_kind = 'referral')
           / nullif(count(d.order_id) filter (where d.is_first_order and d.first_source_kind = 'referral'), 0)
           as cac_referral,
       coalesce(sum(d.referral_discount), 0) as all_discounts
from p
left join fin.v_order_discounts d on d.paid_date_msk >= p.period_start and d.paid_date_msk < p.period_end
group by p.period_type, p.period_start;
comment on view fin.v_referral_cac is
    'CAC реферального канала: скидки на первые заказы клиентов, пришедших по коду друга (first touch referral:*), на одного нового клиента.';

insert into fin.schema_migrations (version) values ('007_referral') on conflict do nothing;
