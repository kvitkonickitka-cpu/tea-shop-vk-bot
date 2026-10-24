-- 009: ответы бухгалтера 21.10.2026 — НДС на плату за чек, факт ЮKassa без двойного учёта,
-- подтверждённые взносы за 2026 и правило даты дохода в КУДиР.
--
-- 1. «Чеки от ЮKassa» — часть комиссии за платёж (публичный тариф: 2,8 % + 1 % за
--    чек), НДС начисляется на всю комиссию. Платёжная нагрузка =
--    (эквайринг + чек) × (1 + НДС), а не эквайринг × (1 + НДС) + чек.
-- 2. Фактическое удержание ЮKassa (amount − income_amount) уже включает чек и НДС —
--    в экономике заказа оно берётся целиком.
-- 3. Взносы за неполный 2026 год — 18 014,08 ₽ до 28.12.2026, расчёт подтверждён.
-- 4. Дата дохода в КУДиР — дата оплаты покупателем (captured_at), записать в учётную политику.
-- Ставку эквайринга (3,4 % или 2,8 %) сверить с тарифом в личном кабинете ЮKassa.

create or replace function fin.payment_rate(on_date date default current_date) returns numeric
    language sql stable as $$
    select (fin.inp('acquiring_rate', on_date)
            + coalesce(fin.inp('fiscal_rate', on_date), 0) * coalesce(fin.inp('fiscal_units', on_date), 1))
         * (1 + coalesce(fin.inp('vat_on_fee', on_date), 0))
$$;
comment on function fin.payment_rate(date) is
    'Платёжная нагрузка: (эквайринг + плата за чек) × (1 + НДС на комиссию). С 009 НДС начисляется и на чек.';

create or replace function fin.payment_rate_for(method text, on_date date default current_date) returns numeric
    language sql stable as $$
    select (coalesce(fin.inp('acquiring_rate_' || coalesce(method, ''), on_date), fin.inp('acquiring_rate', on_date))
            + coalesce(fin.inp('fiscal_rate', on_date), 0) * coalesce(fin.inp('fiscal_units', on_date), 1))
         * (1 + coalesce(fin.inp('vat_on_fee', on_date), 0))
$$;

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
           -- Удержание ЮKassa (amount − income_amount) уже включает плату за чек и НДС:
           -- берём его целиком, 1 % сверху не добавляем (ответ бухгалтера 21.10.2026).
           case when f.fee is not null then f.fee
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

insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select * from (values
    ('ip_fixed_contrib_2026', 'Фиксированные взносы ИП за 2026', 57390::numeric, null::text, '₽/год',
     'Подтверждено бухгалтером 21.10.2026',
     'За неполный 2026 (с 08.09): 3 × 4 782,50 + 23/30 × 4 782,50 = 18 014,08 ₽, срок 28.12.2026. 1 % — только с дохода сверх 300 000 ₽',
     '2026-10-21'::date, 'миграция'),
    ('kudir_income_date', 'КУДиР: дата дохода', null, 'captured_at', 'правило', 'Подтверждено бухгалтером 21.10.2026',
     'Дата оплаты покупателем (ст. 346.17; письмо Минфина 10.02.2020 № 03-11-11/8398). Записать в учётную политику',
     '2026-10-21', 'миграция'),
    ('acquiring_rate', 'Карты: эквайринг без НДС', 0.034, null, '%', 'Сверить с тарифом ЮKassa',
     'Публичный тариф с чеками — 2,8 % + 1 % за чек; если в договоре 2,8 % — поменять эту вводную. Факт — удержание в реестре',
     '2026-10-21', 'миграция')
) as v (key, label, value, value_text, unit, status, note, valid_from, entered_by)
where not exists (select 1 from fin.inputs i where i.key = v.key and i.valid_from = '2026-10-21' and i.entered_by = 'миграция');

update fin.tax_calendar set amount_hint = 18014.08,
       note = 'ИП с 08.09.2026 — пропорционально; расчёт подтверждён бухгалтером 21.10.2026. Налог УСН за 2026 почти наверняка 0: уменьшается на эти взносы'
 where due_date = '2026-12-28' and kind = 'взносы' and status = 'ожидает';

insert into fin.schema_migrations (version) values ('009_accountant_answers') on conflict do nothing;
