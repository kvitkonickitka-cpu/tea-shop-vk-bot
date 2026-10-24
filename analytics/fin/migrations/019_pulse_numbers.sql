-- 019: одна сумма «до старта заплатить», прогноз с планом продаж, сервисы двумя статьями,
-- недели с понедельника, «Товар» — названиями каталога (ревизия «Пульса» 24.10.2026).
--
-- Раньше «сколько ещё заплатить до старта» считалось в трёх местах по-разному: карточка
-- «Запланировано формой» — только планы формы (33 000 ₽), бюджет — остатки статей
-- (135 090 ₽), «Требует внимания» — остатки разовых статей плюс планы (145 409 ₽). Теперь
-- источник один — fin.v_payments_plan, все будущие платежи по дням:
--   остаток статьи бюджета (разовой и рекламы) — на дату её вводной (закупка и доставка
--   партии — tea_purchase_date, ВК — vk_topup_date), без вводной — накануне старта;
--   плановые расходы формы, не закрытые фактом;
--   налоги и взносы из календаря;
--   сервисы, банк и подрядчик — помесячно, первого числа.
-- От него строятся прогноз денег, «Платежи впереди», «До старта заплатить» и строка 1
-- «Требует внимания»: сумма до старта в трёх местах одна.

-- --- сервисы: до старта и за пилот ----------------------------------------------------------
update fin.budget_articles set sort = sort + 1 where sort >= 11 and sort < 99;
insert into fin.budget_articles (item, line_no, kind, cash_class, color, sort)
values ('Сервисы до старта', 17, 'постоянная', 'Операционные', '#7FC8D4', 11)
on conflict (item) do nothing;

-- Траты на сервисы до старта — в свою статью: план пилота их больше не съедает.
create or replace view fin.v_spend as
select source, day, amount,
       case when budget_item = 'Сервисы за пилот' and day < fin.inp_date('pilot_start_date')
            then 'Сервисы до старта' else budget_item end as budget_item,
       cash_class, what, ref
from (
    select 'выписка'::text as source, b.day, b.amount, b.budget_item, b.cash_class, b.article as what, b.operation_id::text as ref
    from fin.v_bank_ops b
    where b.direction = 'out' and b.cash_class in ('Операционные', 'Вложения')
    union all
    select 'мимо счёта', x.date, x.amount, e.budget_item, e.cash_class, x.category, 'expense:' || x.id
    from fin.expenses x
    join fin.v_expense_items e on e.id = x.id
    where not x.is_plan and x.paid_from = 'мимо счёта'
) s;

create or replace view fin.v_pilot_budget as
with purchase as (
    select sum(price_per_unit_base * qty_units_first_purchase) as gross,
           sum(unit_weight_kg * qty_units_first_purchase) as kg
    from fin.sku where active
),
-- 019: сервисы до старта — месяцы, начавшиеся до старта, с месяца первого платежа (нет платежей —
-- с текущего): платят помесячно, первого числа, как в fin.v_payments_plan.
services_before as (
    select greatest(0, (extract(year from st) - extract(year from f)) * 12 + extract(month from st) - extract(month from f)
                       + case when fin.inp_date('pilot_start_date') > st then 1 else 0 end) as months
    from (select date_trunc('month', fin.inp_date('pilot_start_date')) as st,
                 date_trunc('month', coalesce((select min(day) from fin.v_spend
                                               where budget_item in ('Сервисы за пилот', 'Сервисы до старта')),
                                              current_date)) as f) x
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
    -- 019: сервисы до старта — отдельной статьёй: серверы оплачиваются с сентября, и план пилота
    -- (3 месяца с 01.12) был меньше факта ещё до старта. Перенос старта двигает обе статьи.
    union all select 17, 'Сервисы до старта', fin.inp('infra_monthly') * sb.months, fin.inp('infra_monthly') * sb.months,
                         'вводная infra_monthly × месяцы с первого платежа до старта' from services_before sb
    -- 013: декларация о соответствии ТР ТС 021 — нас как изготовителя (упаковщика).
    union all select 16, 'Декларация о соответствии', fin.inp('declaration_cost'), fin.inp('declaration_cost'),
                         'вводная declaration_cost: испытания + пошлина, верхняя оценка'
)
select line_no, item, plan_amount, max_amount, source,
       sum(plan_amount) over () as known_total,
       fin.inp('capital_plan') as capital_plan,
       fin.inp('capital_plan') - sum(plan_amount) over () as left_before_unknown
from lines;

-- --- вводные: план продаж и порог повтора --------------------------------------------------
insert into fin.inputs (key, label, value, unit, status, note, valid_from, entered_by)
select k, l, null, u, 'Нет данных', n, current_date, 'миграция'
from (values
    ('sales_plan_orders_m1', 'План продаж: заказов в М1', 'шт.', 'Для линии «с планом продаж» на «Остаток и прогноз». Пусто — линии нет.'),
    ('sales_plan_orders_m2', 'План продаж: заказов в М2', 'шт.', 'Для линии «с планом продаж».'),
    ('sales_plan_orders_m3', 'План продаж: заказов в М3', 'шт.', 'Для линии «с планом продаж».'),
    ('sales_mix_sets', 'План продаж: доля наборов', '%', 'Доля наборов 4×35 г в плановых заказах (доли трёх видов в сумме — 100 %).'),
    ('sales_mix_sets100', 'План продаж: доля сетов', '%', 'Доля сетов 2×100 г в плановых заказах.'),
    ('sales_mix_packs', 'План продаж: доля пачек', '%', 'Доля пачек и блинов в плановых заказах.')
) v (k, l, u, n)
where not exists (select 1 from fin.inputs i where i.key = v.k);

insert into fin.inputs (key, label, value, unit, status, note, valid_from, entered_by)
select 'gate_m3_repeat45_target', 'Ворота М3: доля повторов за 45 дней у первой когорты', 0.15, '%', 'Допущение',
       'По умолчанию 15 % — ревизия «Пульса» 24.10.2026; уточнить по первой когорте', current_date, 'миграция'
where (select value from fin.inputs_current where key = 'gate_m3_repeat45_target') is null;

-- --- все будущие платежи по дням ------------------------------------------------------------
create or replace view fin.v_payments_plan as
with s as (
    select fin.inp_date('pilot_start_date') as start,
           fin.pilot_month_start(coalesce(fin.inp('pilot_months')::int, 3) + 1) as pilot_end
),
h as (select greatest(s.pilot_end + 30, current_date + 56) as horizon from s),
budget as (
    select b.item, b.left_amount,
           case when b.item in ('Первая закупка чая', 'Доставка основной партии') then fin.inp_date('tea_purchase_date')
                when b.item = 'ВК: пополнение кабинета' then fin.inp_date('vk_topup_date') end as input_day
    from fin.v_pilot_budget_status b
    where b.kind in ('разовая', 'реклама') and b.left_amount > 0
),
months as (
    select m::date as day from h, generate_series(date_trunc('month', current_date) + interval '1 month', h.horizon,
                                                   interval '1 month') m
)
select greatest(coalesce(b.input_day, s.start - 1), current_date + 1) as day,
       coalesce(b.input_day, s.start - 1) as plan_date, 'остаток статьи «' || b.item || '»' as what,
       round(b.left_amount, 2) as amount, b.item as budget_item, 'статья'::text as source, false as closed,
       coalesce(b.input_day, s.start - 1) < current_date as overdue
from budget b cross join s
union all
select p.due_day, p.plan_date, p.what, p.amount, p.budget_item, 'план', false, p.overdue
from fin.v_plans p where not p.closed
union all
select greatest(t.due_date, current_date + 1), t.due_date, t.event, t.amount_hint, null, 'налоги', false,
       t.due_date < current_date
from fin.tax_calendar t
where t.status = 'ожидает' and t.kind in ('взносы', 'взносы_1%', 'налог', 'аванс')
union all
select m.day, m.day, 'сервисы за месяц', fin.inp('infra_monthly'),
       case when m.day < s.start then 'Сервисы до старта' else 'Сервисы за пилот' end, 'сервисы', false, false
from months m cross join s where coalesce(fin.inp('infra_monthly'), 0) > 0
union all
select m.day, m.day, 'банк за месяц', fin.inp('bank_fee_monthly'), 'Обслуживание банка за пилот', 'банк', false, false
from months m where m.day >= fin.inp_date('bank_paid_from') and coalesce(fin.inp('bank_fee_monthly'), 0) > 0
union all
select m.m_start, m.m_start, 'подрядчик за месяц', fin.contractor_monthly(), 'Подрядчик за пилот', 'подрядчик', false, false
from (select fin.pilot_month_start(n) as m_start from generate_series(1, coalesce(fin.inp('pilot_months')::int, 3)) n) m
where m.m_start > current_date and coalesce(fin.contractor_monthly(), 0) > 0;
comment on view fin.v_payments_plan is
    'Все будущие платежи по дням — один источник для прогноза, «Платежи впереди», «До старта заплатить» и «Требует внимания». '
    'Остаток статьи — на дату её вводной, без вводной — накануне старта; планы формы, налоги, сервисы, банк, подрядчик.';

-- Платежи впереди: те же платежи на 8 недель и закрытые планы за неделю — видно, что закрылось.
create or replace view fin.v_payments_ahead as
select p.day, p.plan_date, p.what, p.amount::numeric(14,2), p.budget_item, p.source, p.closed, p.overdue
from fin.v_payments_plan p where p.day <= current_date + 56
union all
select p.closed_on, p.plan_date, p.what, p.amount, p.budget_item, 'план', true, false
from fin.v_plans p where p.closed and p.plan_date >= current_date - 7;

-- --- прогноз остатка ------------------------------------------------------------------------
-- 019: платежи — из fin.v_payments_plan; добавлена линия «с планом продаж» (closing_with_sales):
-- плановые заказы в день × средняя маржа до рекламы по плановому миксу минус реклама сверх уже
-- оплаченного пополнения ВК (пополнение — в платежах, второй раз не списываем). До старта и в
-- прошедшие дни линии нечего добавить: остаток на сегодня уже фактический.
create or replace view fin.v_runway_daily as
with s as (
    select (select balance from finance.v_daily_balance_total order by day desc limit 1)
           - coalesce((select sum(holds_out) from finance.v_pending_summary), 0) as balance,
           fin.inp_date('pilot_start_date') as pilot_start,
           fin.pilot_month_start(coalesce(fin.inp('pilot_months')::int, 3) + 1) as pilot_end
),
capital as (
    select c.remaining as amount,
           greatest(coalesce(fin.inp_date('capital_expected_date'), current_date + 1), current_date + 1) as day
    from fin.v_capital_status c
),
h as (select greatest(s.pilot_end + 30, current_date + 56) as horizon from s),
days as (select d::date as day from h, generate_series(current_date + 1, h.horizon, interval '1 day') d),
pay as (
    select day,
           sum(amount) filter (where source = 'статья' and budget_item <> 'ВК: пополнение кабинета') as budget_out,
           sum(amount) filter (where source = 'статья' and budget_item = 'ВК: пополнение кабинета') as ads_out,
           sum(amount) filter (where source in ('налоги', 'сервисы', 'банк', 'подрядчик')) as fixed_out,
           sum(amount) filter (where source = 'план') as plans_out
    from fin.v_payments_plan group by day
),
margin as (
    select avg(margin_before_cac) filter (where kind = 'набор') as sets,
           avg(margin_before_cac) filter (where kind = 'сет') as sets100,
           avg(margin_before_cac) filter (where kind in ('пачка', 'блин')) as packs
    from fin.v_unit_cost where retail_price is not null
),
months as (
    select n, fin.pilot_month_start(n) as m_start, fin.pilot_month_start(n + 1) as m_end,
           fin.inp('sales_plan_orders_m' || n) as orders,
           -- Реклама месяца сверх уже оплаченного пополнения ВК: оно покрывает первые месяцы.
           greatest(0, sum(coalesce(fin.inp('ad_plan_m' || n), 0)) over (order by n) - coalesce(fin.inp('vk_topup_own'), 0))
           - greatest(0, sum(coalesce(fin.inp('ad_plan_m' || n), 0)) over (order by n)
                         - coalesce(fin.inp('ad_plan_m' || n), 0) - coalesce(fin.inp('vk_topup_own'), 0)) as ads_extra
    from generate_series(1, coalesce(fin.inp('pilot_months')::int, 3)) n
),
plan as (
    select d.day,
           (m.orders * (coalesce(fin.inp('sales_mix_sets'), 0) * mg.sets + coalesce(fin.inp('sales_mix_sets100'), 0) * mg.sets100
                        + coalesce(fin.inp('sales_mix_packs'), 0) * mg.packs) - m.ads_extra)
           / (m.m_end - m.m_start) as net
    from days d join months m on d.day >= m.m_start and d.day < m.m_end cross join margin mg
),
plan_ok as (
    select bool_and(orders is not null) and fin.inp('sales_mix_sets') is not null as ok from months
),
flows as (
    select d.day,
           case when d.day = c.day then c.amount else 0 end as capital_in,
           coalesce(y.amount, 0) as payouts_in,
           coalesce(p.budget_out, 0) as budget_out,
           coalesce(p.ads_out, 0) as ads_out,
           coalesce(p.fixed_out, 0) as fixed_out,
           coalesce(p.plans_out, 0) as plans_out,
           coalesce(pl.net, 0) as plan_net
    from days d cross join capital c
    left join fin.v_yk_transit_daily y on y.day = d.day
    left join pay p on p.day = d.day
    left join plan pl on pl.day = d.day
)
select f.day, f.capital_in, f.payouts_in, f.budget_out, f.ads_out, f.fixed_out,
       s.balance + sum(f.capital_in + f.payouts_in - f.budget_out - f.ads_out - f.fixed_out - f.plans_out)
           over (order by f.day) as closing,
       fin.inp('min_cash_reserve') as reserve,
       s.balance + sum(f.payouts_in - f.budget_out - f.ads_out - f.fixed_out - f.plans_out) over (order by f.day)
           as closing_no_capital,
       f.plans_out,
       -- новый столбец (019)
       case when (select ok from plan_ok) then
           s.balance + sum(f.capital_in + f.payouts_in - f.budget_out - f.ads_out - f.fixed_out - f.plans_out + f.plan_net)
               over (order by f.day) end as closing_with_sales
from flows f cross join s;

-- Недели с понедельника: первая — от завтра до воскресенья.
create or replace view fin.v_weekly_forecast as
with w as (
    select date_trunc('week', day)::date as monday, min(day) as week_start,
           sum(capital_in + payouts_in) as inflow, sum(budget_out + ads_out + fixed_out + plans_out) as outflow,
           (array_agg(closing order by day desc))[1] as closing, min(closing) as lowest, max(reserve) as reserve
    from fin.v_runway_daily group by 1
),
n as (select *, (row_number() over (order by monday))::int as week_no from w)
select week_no, week_start, closing - inflow + outflow as opening, inflow, outflow, closing, lowest,
       lowest < reserve as below_reserve
from n where week_no <= 8 order by week_no;

create or replace view fin.v_money_now as
with b as (
    select day, balance from finance.v_daily_balance_total order by day desc limit 1
),
r as (select min(day) filter (where closing < reserve) as runout,
             min(day) filter (where closing_no_capital < reserve) as runout_no_capital from fin.v_runway_daily),
hz as (select max(day) as horizon from fin.v_runway_daily)
select b.day as bank_date, b.balance as bank_balance, fin.inp('min_cash_reserve') as reserve,
       b.balance - fin.inp('min_cash_reserve') as free_cash,
       r.runout as runout_date, hz.horizon,
       fin.pilot_month_start(coalesce(fin.inp('pilot_months')::int, 3) + 1) as pilot_end,
       case when b.balance < fin.inp('min_cash_reserve') then b.day else r.runout_no_capital end as runout_no_capital,
       (select remaining from fin.v_capital_status) as capital_remaining,
       -- 019: все платежи до старта из fin.v_payments_plan — та же сумма, что «До старта заплатить».
       (select coalesce(sum(amount), 0) from fin.v_payments_plan where day < fin.inp_date('pilot_start_date'))
           as left_before_start,
       -- 019: коротко — помещается в плитку шириной 6 при окне 1280: «31.03.2027» или «позже 31.03.27».
       case when b.balance < fin.inp('min_cash_reserve') then 'уже ниже резерва'
            when r.runout is not null then to_char(r.runout, 'DD.MM.YYYY')
            else 'позже ' || to_char(hz.horizon, 'DD.MM.YY') end as runway_text
from b cross join r cross join hz;

create or replace view fin.v_balance_chart as
with fact as (
    select day, balance from finance.v_daily_balance_total
    where day >= (select min(day) from finance.v_daily_balance_total where balance <> 0)
),
last_fact as (select day, balance from fact order by day desc limit 1),
rows_ as (
    select day, balance as actual, null::numeric as forecast, null::numeric as forecast_no_capital,
           null::numeric as forecast_with_sales from fact
    union all
    select day, null, balance, balance, case when exists (select 1 from fin.v_runway_daily where closing_with_sales is not null)
                                             then balance end from last_fact
    union all
    select day, null, closing, closing_no_capital, closing_with_sales from fin.v_runway_daily
)
select day, max(actual) as actual, max(forecast) as forecast, fin.inp('min_cash_reserve') as reserve,
       max(forecast_no_capital) as forecast_no_capital,
       -- новый столбец (019)
       max(forecast_with_sales) as forecast_with_sales
from rows_ group by day;

-- --- «Товар»: названия, цена и вид — из каталога ------------------------------------------
create or replace view fin.v_product_card as
select u.product, coalesce(l.name, n.display_name, u.product) as name, n.supplier_code,
       coalesce(case c.kind when 'сет' then 'сет' when 'набор' then 'набор' when 'блин' then 'блин' when 'пачка' then 'пачка' end,
                u.kind) as kind,
       case coalesce(c.kind, u.kind) when 'набор' then 1 when 'сет' then 2 when 'пачка' then 3 else 4 end as kind_sort,
       case coalesce(c.kind, u.kind) when 'набор' then 'Наборы' when 'сет' then 'Сеты' when 'пачка' then 'Пачки' else 'Блины' end
           as kind_group,
       u.retail_price, u.full_cost, u.margin_before_cac, u.margin_pct,
       u.margin_pct < coalesce(fin.inp('low_margin_threshold'), 0.20) as low_margin,
       coalesce(l.on_sale, false) as sold_by_bot,
       -- новый столбец (019)
       l.sku_id
from fin.v_unit_cost u
left join fin.v_catalog_link l on l.product = u.product
left join shop.catalog c on c.sku_id = l.sku_id
left join fin.product_names n on n.product = u.product;

-- Ворота: откуда порог — вводная (для подписи на карточке).
create or replace view fin.v_pilot_gates as
with s as (
    select fin.inp_date('pilot_start_date') as start,
           (select period_start from fin.v_pilot_months where month_no = 1) as m1_start,
           (select period_end from fin.v_pilot_months where month_no = 1) as m1_end
),
w2 as (
    select count(*) as dialogs, count(*) filter (where d.is_paid) as paid
    from analytics.v_dialogs d cross join s
    where d.session_source_kind = 'ad' and d.started_date_msk >= s.start and d.started_date_msk < s.start + 14
),
first_ad as (
    -- Новые клиенты из рекламы (first touch ad:*): первый оплаченный заказ.
    select e.client_key, e.order_id, e.paid_date_msk, e.margin_before_cac, e.usn_reserve
    from fin.v_order_economics e join analytics.v_clients c using (client_key)
    where e.is_first_order and c.first_source_kind = 'ad'
),
w6 as (
    select (select sum(spend_own) from fin.ad_spend a cross join s where a.date >= s.start and a.date < s.start + 42) as spend,
           count(*) as new_clients, avg(f.margin_before_cac + f.usn_reserve) as first_margin
    from first_ad f cross join s where f.paid_date_msk >= s.start and f.paid_date_msk < s.start + 42
),
cohort as (
    select f.client_key, f.paid_date_msk as first_paid,
           exists (select 1 from fin.v_order_economics r where r.client_key = f.client_key and not r.is_first_order
                     and r.paid_date_msk <= f.paid_date_msk + 45) as repeat45,
           (select sum(r.margin_before_cac + r.usn_reserve) from fin.v_order_economics r
             where r.client_key = f.client_key and r.paid_date_msk <= f.paid_date_msk + 90) as margin90
    from first_ad f cross join s where f.paid_date_msk >= s.m1_start and f.paid_date_msk < s.m1_end
),
m1_cac as (
    select (select sum(spend_own) from fin.ad_spend a cross join s where a.date >= s.m1_start and a.date < s.m1_end)
           / nullif((select count(*) from cohort), 0) as cac
),
checks (sort, gate, check_name, value, threshold, higher_is_better, period_end, unit, threshold_key) as (
    select 1, 'Неделя 2', 'Конверсия платного диалога в оплату',
           w2.paid::numeric / nullif(w2.dialogs, 0), fin.inp('gate_w2_conv_min'), true, s.start + 14, '%', 'gate_w2_conv_min'
    from w2 cross join s
    union all
    select 2, 'Неделя 6', 'Платный CAC', w6.spend / nullif(w6.new_clients, 0), fin.inp('gate_w6_cac_max'), false,
           s.start + 42, '₽', 'gate_w6_cac_max' from w6 cross join s
    union all
    select 3, 'Неделя 6', 'Маржа первого заказа после CAC',
           w6.first_margin - w6.spend / nullif(w6.new_clients, 0), fin.inp('gate_w6_first_margin_min'), true,
           s.start + 42, '₽', 'gate_w6_first_margin_min' from w6 cross join s
    union all
    select 4, 'М3', 'Повтор за 45 дней у первой когорты',
           (select avg(case when repeat45 then 1 else 0 end) from cohort), fin.inp('gate_m3_repeat45_target'), true,
           (select period_end from fin.v_pilot_months where month_no = 3), '%', 'gate_m3_repeat45_target' from s
    union all
    select 5, 'М3', 'Прогноз: маржа за 90 дней − CAC',
           (select avg(margin90) from cohort) - (select cac from m1_cac), fin.inp('gate_m3_ltv90_min'), true,
           (select period_end from fin.v_pilot_months where month_no = 3), '₽', 'gate_m3_ltv90_min' from s
    union all
    select 6, 'М3', 'Операционная прибыль М3',
           (select operating_profit from fin.v_pnl_monthly where month_no = 3), fin.inp('gate_m3_profit_min'), true,
           (select period_end from fin.v_pilot_months where month_no = 3), '₽', 'gate_m3_profit_min' from s
)
select gate, check_name, value, threshold, unit,
       case when value is null or threshold is null then 'нет данных'
            when (higher_is_better and value >= threshold) or (not higher_is_better and value <= threshold) then 'зелёный'
            else 'красный' end as status,
       period_end, current_date >= period_end as period_complete, sort,
       -- новый столбец (019)
       threshold_key
from checks order by sort;

create or replace view fin.v_attention as
with ps as (select * from fin.v_pilot_status),
money as (select * from fin.v_money_now),
checks as (
    select 1 as sort,
           case when m.free_cash + m.capital_remaining < m.left_before_start then 'Срочно' else 'Важно' end as level,
           'Денег не хватает до старта: осталось потратить до старта ' || fin.fmt_rub(m.left_before_start)
               || ', свободно ' || fin.fmt_rub(m.free_cash)
               || case when m.capital_remaining > 0 then ', ждём вкладов ' || fin.fmt_rub(m.capital_remaining) else '' end as text,
           'money' as place
    from money m cross join ps
    where not ps.started and m.free_cash < m.left_before_start
    union all
    select 2, 'Важно', 'Не разнесены покупки с карты: переведено себе на покупки ' || fin.fmt_rub(r.transferred)
               || ', внесено «мимо счёта» ' || fin.fmt_rub(r.recorded) || ', разница ' || fin.fmt_rub(r.unreconciled),
           'entry'
    from fin.v_card_reconcile_status r where r.overdue
    union all
    select 3, 'Важно', 'Переводы себе без разметки: ' || count(*) || ' на ' || fin.fmt_rub(sum(amount))
               || ' — по умолчанию считаются покупками, проверьте', 'checks'
    from fin.v_bank_ops where transfer_kind_default having count(*) > 0
    union all
    select 4, 'Важно', 'Разметить ' || count(*) || ' операц. на ' || fin.fmt_rub(sum(amount)) || ': статья бюджета не определена',
           'checks'
    from fin.v_bank_ops where direction = 'out' and budget_item = 'Не сопоставлено' having count(*) > 0
    union all
    select 5, 'Важно', 'Вклад ' || fin.fmt_rub(contributed) || ' не привязан к партнёру', 'checks'
    from fin.v_partner_capital where partner = 'Не указано, чей'
    union all
    select 6, case when ps.days_to_start < 7 then 'Срочно' else 'Важно' end,
           'Партия чая не внесена, до старта ' || ps.days_to_start || ' дн.', 'entry'
    from ps where not ps.started and ps.days_to_start < coalesce(fin.inp('tea_lot_warn_days')::int, 21)
      and not exists (select 1 from fin.purchase_lots)
    union all
    select 7, 'Важно', 'Не разобрано операций выписки: ' || count(*) || ' на ' || fin.fmt_rub(sum(amount)), 'checks'
    from fin.v_bank_ops where article_id = 'tech_unclassified' having count(*) > 0
    union all
    select 8, 'Важно', 'Снимок остатка ' || to_char(c.snapshot_date, 'DD.MM.YYYY') || ' расходится с банком на '
               || fin.fmt_rub(c.snapshot_diff), 'checks'
    from fin.v_cash_status c where coalesce(c.snapshot_diff, 0) <> 0
    union all
    select 9, 'Важно', 'Claude: ботом не учтено ' || fin.fmt_rub(s.bill_rub - s.bot_rub) || ' из ' || fin.fmt_rub(s.bill_rub)
               || ' счёта Anthropic (' || fin.fmt_pct(s.unaccounted_share) || ') за ' || s.days || ' дн.', 'checks'
    from fin.v_claude_status s
    where s.days >= 3 and s.unaccounted_share > coalesce(fin.inp('claude_unaccounted_max'), 0.20)
    union all
    select 10, 'Срочно', 'Чек не зарегистрирован у ЮKassa: ' || r.payments || ' оплат(ы)', 'checks'
    from fin.v_yk_receipts_missing r
    union all
    select 11, 'Важно', 'Выплаты ЮKassa не сходятся с реестром: ' || count(*) || ' на ' || fin.fmt_rub(sum(abs(diff))), 'checks'
    from fin.v_yk_payouts where status = 'mismatch' having count(*) > 0
    union all
    select 12, 'Срочно', 'Оплачены и не переданы перевозчику дольше суток: ' || count(*) || ' заказ(ов)', 'sales'
    from analytics.v_orders o cross join ps
    where ps.started and o.is_paid and not o.is_refunded and not o.is_canceled and o.paid_date_msk < current_date
      and not exists (select 1 from analytics.v_funnel_events e where e.order_id = o.order_id
                      and e.event in ('handed_over', 'at_pickup_point', 'delivered', 'not_delivered'))
    having count(*) > 0
    union all
    select 13, 'Важно', 'Вопросы менеджеру без ответа: ' || count(*), 'sales'
    from (
        select distinct on (client_key) client_key, event from analytics.v_funnel_events
        where event in ('escalation_opened', 'escalation_closed') and client_key is not null
        order by client_key, created_at desc
    ) e cross join ps
    where ps.started and e.event = 'escalation_opened'
    having count(*) > 0
    union all
    -- 013: плановый платёж прошёл, а факта к нему нет.
    select 14, 'Важно', 'План ' || to_char(p.plan_date, 'DD.MM.YYYY') || ' «' || p.what || '» ' || fin.fmt_rub(p.amount)
               || ' не закрыт фактом: оплачено — отметьте «Закрывает план» в расходе или разметке операции, '
               || 'нет — перенесите дату (в прогнозе стоит на завтра)', 'entry'
    from fin.v_plans p where p.overdue
    union all
    -- 014: склад по партиям.
    select 15, 'Важно', 'Пора докупать: ' || coalesce(n.display_name, r.raw_material) || ' — свободно ' || r.free_shown
               || ', хватит на ' || coalesce(round(r.days_left)::text, '?') || ' дн.; заказать ' || r.order_shown, 'stock'
    from fin.v_reorder_raw r left join fin.product_names n on n.product = r.raw_material
    where r.order_now
    union all
    select 16, case when e.days_left < 30 then 'Срочно' else 'Важно' end,
           'Партия ' || e.lot_code || ' годна до ' || to_char(e.best_before, 'DD.MM.YYYY') || ', осталось '
               || trim_scale(e.on_hand)::text || ' ' || e.unit, 'stock'
    from fin.v_lots_expiring e
    union all
    select 17, 'Важно', 'Включено, но нет сырья: ' || string_agg(x.sku_id || ' «' || x.name || '»', ', ')
               || ' — бот не продаёт', 'stock'
    from fin.v_on_sale_no_stock x having count(*) > 0
    union all
    select 18, 'Срочно', 'Заказ №' || r.order_id || ': не хватило сырья «' || r.raw_material
               || '» — внесите партию, иначе пачку не собрать', 'stock'
    from (select distinct order_id, raw_material from shop.reservations where status = 'резерв' and lot_id is null) r
    union all
    -- 015: коды «Честного знака».
    select 19, 'Срочно', 'Нельзя продавать: ' || count(*) || ' код(ов) наклеены и не введены в оборот дольше рабочего дня — заказы '
               || string_agg(distinct '№' || order_id, ', '), 'codes'
    from fin.v_codes_unsellable having count(*) > 0
    union all
    select 20, 'Важно', 'Застряли: ' || count(*) || ' код(ов) введены в оборот и не проданы дольше 3 дней — пачка потеряна '
               || 'или не отправлена', 'codes'
    from fin.v_codes_stuck having count(*) > 0
    union all
    select 22, 'Важно', 'Декларация в выгрузке кодов — тестовая («' || value_text || '»): в ЛК ЧЗ такую выгрузку не '
               || 'подавать, сначала настоящий номер и дату во «Вводных»', 'codes'
    from fin.inputs_current cross join ps
    where key = 'chz_declaration_number' and value_text ilike '%тест%'
      -- 019: только когда она вот-вот пойдёт в дело — есть что выгружать или до старта меньше 14 дней;
      -- иначе это ежедневный шум: своей декларации ещё нет намеренно.
      and (exists (select 1 from shop.v_code_status where status = 'applied')
           or exists (select 1 from shop.code_batches where status = 'выгружена')
           or (not ps.started and ps.days_to_start < 14))
    union all
    select 21, case when r.days_left < 7 then 'Срочно' else 'Важно' end,
           'Кодов ЧЗ хватит на ' || round(r.days_left) || ' дн.: GTIN ' || r.gtin || coalesce(' «' || r.name || '»', '')
               || ' — свободно ' || r.free || ', закажите в СУЗ', 'codes'
    from fin.v_codes_runway r where r.days_left < 14
    union all
    -- 016: себестоимость считается по составу из каталога — сырьё должно быть в справочнике.
    select 23, 'Важно', 'Нет себестоимости: сырьё ' || string_agg(distinct '«' || x.raw_material || '»', ', ')
               || ' не найдено в справочнике сырья финмодели — артикулы ' || string_agg(distinct x.sku_id, ', ')
               || '. Исправьте «Сырьё» в каталоге', 'catalog'
    from fin.v_catalog_no_cost x having count(*) > 0
    union all
    -- 019: платёж в день ожидаемых вкладов или раньше, а без вкладов денег на него нет.
    select 24, 'Срочно', 'Платёж ' || fin.fmt_rub(p.amount) || ' (' || p.what || ') назначен на '
               || to_char(p.day, 'DD.MM') || ' — на дату ожидаемых вкладов или раньше, свободных денег на него нет: '
               || 'внесите вклады до ' || to_char(p.day - 1, 'DD.MM') || ' или сдвиньте платёж', 'money'
    from money m
    cross join lateral (select * from fin.v_payments_plan x where x.day = m.runout_no_capital
                        order by x.amount desc limit 1) p
    where m.capital_remaining > 0
      and m.runout_no_capital <= greatest(coalesce(fin.inp_date('capital_expected_date'), current_date + 1), current_date + 1)
)
select sort, level, text, place from checks
union all
select 0, 'Всё в порядке', 'Проверки ничего не нашли', null where not exists (select 1 from checks)
order by 1;

insert into fin.schema_migrations (version) values ('019_pulse_numbers') on conflict do nothing;
