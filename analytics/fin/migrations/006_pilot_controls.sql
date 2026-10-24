-- 006: контроль пилота — налоги без двойного учёта, факт ЮKassa, точка заказа,
-- прогноз денег на 8 недель, ворота пилота, акты перевозчиков, налоговый календарь.
--
-- Пакет A (A1, A5, A6, A8–A13) разбора процессов от 20.10.2026. Правила те же:
-- столбцы существующих представлений только дописываются в конец (на них
-- стоят карточки Metabase), вводные — новыми строками с историей, «нет
-- данных» — NULL. Реестр ЮKassa (finance.yk_*) и КУДиР (finance.v_yk_kudir)
-- ведёт бот; здесь они только читаются. Применять после деплоя бота с
-- реестром (PR #30) и столбцами carrier_actual_cost / carrier_track в
-- analytics.v_orders.

-- Строки, которые вносит миграция или бот (акт перевозчика командой /act), а не человек.
create or replace function fin.valid_author(name text) returns boolean
    language sql immutable as $$ select name in ('Никита', 'Илья', 'импорт v1.5', 'миграция', 'бот') $$;

-- --- вводные ---------------------------------------------------------------------
insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select * from (values
    -- A6: взносы и налог. Сумма за 2026 — прежняя, но со статусом «сверить».
    ('ip_fixed_contrib_2026', 'Фиксированные взносы ИП за 2026', 57390::numeric, null::text, '₽/год', 'Сверить с бухгалтером',
     'Полный 2026 год; ИП с 08.09.2026 платит пропорционально — сумму к уплате (fin.tax_calendar) сверить с бухгалтером',
     '2026-10-20'::date, 'миграция'),
    ('ip_fixed_contrib_2026_due', 'Срок уплаты фиксированных взносов 2026', null, '2026-12-28', 'дата', 'Официально',
     'НК РФ ст. 432: не позднее 28 декабря', '2026-10-20', 'миграция'),
    ('ip_extra_contrib_rate', 'Дополнительный взнос с дохода свыше порога', 0.01, null, '%', 'Официально',
     '1 % с дохода свыше 300 000 ₽ за год (ip_extra_contrib_threshold)', '2026-10-20', 'миграция'),
    ('ip_extra_contrib_due_2026', 'Срок уплаты взноса 1 % за 2026', null, '2027-07-01', 'дата', 'Официально',
     'Не позднее 1 июля следующего года', '2026-10-20', 'миграция'),
    -- A8: эквайринг по способам оплаты. Пусто — ставка карт (acquiring_rate).
    ('acquiring_rate_bank_card', 'Эквайринг: банковская карта', 0.034, null, '%', 'Никита 13.10: 3,4%',
     'Без НДС на комиссию — он отдельно (vat_on_fee)', '2026-10-20', 'миграция'),
    ('acquiring_rate_sbp', 'Эквайринг: СБП', null, null, '%', 'Нет данных', 'Тариф ЮKassa для СБП — уточнить в договоре', '2026-10-20', 'миграция'),
    ('acquiring_rate_sberbank', 'Эквайринг: SberPay', null, null, '%', 'Нет данных', 'Тариф ЮKassa для SberPay — уточнить', '2026-10-20', 'миграция'),
    ('acquiring_rate_tinkoff_bank', 'Эквайринг: T-Pay', null, null, '%', 'Нет данных', 'Тариф ЮKassa для T-Pay — уточнить', '2026-10-20', 'миграция'),
    ('acquiring_rate_yoo_money', 'Эквайринг: ЮMoney', null, null, '%', 'Нет данных', 'Тариф ЮKassa для ЮMoney — уточнить', '2026-10-20', 'миграция'),
    ('planning_payment_method', 'Способ оплаты для плановой юнитки', null, 'bank_card', 'способ', 'Предположение',
     'Модель (fin.unit_costs) считает эквайринг по ставке этого способа', '2026-10-20', 'миграция'),
    -- A9: безубыточность по факту.
    ('breakeven_fact_days', 'Безубыточность: окно факта', 14, null, 'дней', 'Решение 20.10', 'За сколько дней брать фактическую маржу', '2026-10-20', 'миграция'),
    ('breakeven_fact_min_orders', 'Безубыточность: заказов для факта', 10, null, 'заказов', 'Решение 20.10',
     'Меньше — делим на маржу lead-продукта', '2026-10-20', 'миграция'),
    -- A10: точка заказа.
    ('supply_lead_days', 'Срок поставки чая (по умолчанию)', null, null, 'дней', 'Нет данных',
     'Свой срок у SKU или поставщика — таблица fin.supply_lead_times', '2026-10-20', 'миграция'),
    ('packing_marking_days', 'Дни на фасовку и маркировку', null, null, 'дней', 'Нет данных', null, '2026-10-20', 'миграция'),
    ('safety_stock_days', 'Страховой запас', null, null, 'дней', 'Нет данных', null, '2026-10-20', 'миграция'),
    -- A11: прогноз денег.
    ('ad_daily_budget', 'Реклама: дневной бюджет по плану', null, null, '₽/день', 'Нет данных',
     'Свои деньги в день; пополнение кабинета — отдельным расходом-планом', '2026-10-20', 'миграция'),
    ('ad_campaign_start', 'Реклама: начало кампании', null, '2026-11-15', 'дата', 'Решение 20.10', 'pilot_start_date', '2026-10-20', 'миграция'),
    ('ad_campaign_end', 'Реклама: конец кампании', null, null, 'дата', 'Нет данных', 'Пусто — до конца горизонта прогноза', '2026-10-20', 'миграция'),
    -- A12: ворота пилота.
    ('gate_w2_conv_min', 'Ворота нед. 2: конверсия платного диалога в оплату', 0.15, null, '%', 'Решение 20.10', null, '2026-10-20', 'миграция'),
    ('gate_w6_cac_max', 'Ворота нед. 6: платный CAC не выше', 700, null, '₽', 'Решение 20.10', null, '2026-10-20', 'миграция'),
    ('gate_w6_first_margin_min', 'Ворота нед. 6: маржа первого заказа после CAC не ниже', 0, null, '₽', 'Решение 20.10', null, '2026-10-20', 'миграция'),
    ('gate_m3_repeat45_target', 'Ворота М3: доля повторов за 45 дней у первой когорты', null, null, '%', 'Нет данных',
     'Целевую долю задают владельцы', '2026-10-20', 'миграция'),
    ('gate_m3_ltv90_min', 'Ворота М3: маржа за 90 дней − CAC не ниже', 0, null, '₽', 'Решение 20.10', null, '2026-10-20', 'миграция'),
    ('gate_m3_profit_min', 'Ворота М3: операционная прибыль М3 не ниже', 0, null, '₽', 'Решение 20.10', null, '2026-10-20', 'миграция'),
    -- A13: сверка доставки.
    ('carrier_deviation_max', 'Доставка: допустимое отклонение факта от котировки', 0.10, null, '%', 'Решение 20.10', null, '2026-10-20', 'миграция')
) as v (key, label, value, value_text, unit, status, note, valid_from, entered_by)
where not exists (select 1 from fin.inputs i where i.key = v.key and i.entered_by = 'миграция');

-- --- таблицы ввода ----------------------------------------------------------------

-- B4 / A6 / A11: налоговый календарь. Отметка «оплачено/подано» — командой /tax
-- в чате Ops или формой. paid_amount уплаченных взносов уменьшает УСН (fin.usn_tax_ytd).
create table if not exists fin.tax_calendar (
    id          bigserial primary key,
    due_date    date          not null,
    event       text          not null,
    kind        text          not null check (kind in ('взносы', 'взносы_1%', 'налог', 'аванс', 'декларация', 'уведомление')),
    amount_hint numeric(14,2),
    status      text          not null default 'ожидает' check (status in ('ожидает', 'оплачено', 'подано')),
    paid_amount numeric(14,2),
    done_on     date,
    done_by     text,
    note        text,
    entered_by  text          not null default 'миграция' check (fin.valid_author(entered_by)),
    created_at  timestamptz   not null default now(),
    unique (due_date, event)
);
comment on table fin.tax_calendar is
    'Налоговый календарь ИП: срок, событие, ориентир суммы, статус. Уплаченные взносы (kind взносы, status оплачено) уменьшают резерв УСН.';

insert into fin.tax_calendar (due_date, event, kind, amount_hint, note)
select * from (values
    ('2026-12-28'::date, 'Фиксированные взносы ИП за 2026', 'взносы',
     -- Неполный год с даты регистрации (лист 1, строка 65): ориентир, сверить с бухгалтером.
     round(fin.inp('ip_fixed_contrib_2026') / 12 * (12 - extract(month from date '2026-09-08')
         + (30 - 8 + 1) / 30.0), 2),
     'ИП с 08.09.2026 — пропорционально; сверить сумму с бухгалтером'),
    ('2027-04-25', 'Декларация УСН за 2026', 'декларация', null::numeric, 'ИП: до 25 апреля'),
    ('2027-04-28', 'Налог УСН за 2026', 'налог', null, 'Сумма — fin.v_pnl_monthly / резерв УСН за 2026'),
    ('2027-04-25', 'Уведомление об авансе УСН за I квартал 2027', 'уведомление', null, null),
    ('2027-04-28', 'Аванс УСН за I квартал 2027', 'аванс', null, null),
    ('2027-07-01', 'Взнос 1 % с дохода свыше 300 000 ₽ за 2026', 'взносы_1%', null, 'Если доход 2026 больше 300 000 ₽'),
    ('2027-07-25', 'Уведомление об авансе УСН за полугодие 2027', 'уведомление', null, null),
    ('2027-07-28', 'Аванс УСН за полугодие 2027', 'аванс', null, null),
    ('2027-10-25', 'Уведомление об авансе УСН за 9 месяцев 2027', 'уведомление', null, null),
    ('2027-10-28', 'Аванс УСН за 9 месяцев 2027', 'аванс', null, null),
    ('2027-12-28', 'Фиксированные взносы ИП за 2027', 'взносы', 61154, 'ip_fixed_contrib_2027')
) as v (due_date, event, kind, amount_hint, note)
on conflict (due_date, event) do nothing;

-- A10: срок поставки по SKU или поставщику (перекрывает supply_lead_days). История — новой строкой.
create table if not exists fin.supply_lead_times (
    id          bigserial primary key,
    scope       text          not null check (scope in ('sku', 'поставщик')),
    name        text          not null,
    lead_days   integer       not null check (lead_days >= 0),
    valid_from  date          not null default current_date,
    note        text,
    entered_by  text          not null check (fin.valid_author(entered_by)),
    created_at  timestamptz   not null default now()
);

-- A13: акты перевозчиков — факт стоимости по отправлению (форма «Акт перевозчика»
-- или CSV командой /act в чате Ops). kind «возврат» — обратная доставка при невыкупе.
create table if not exists fin.carrier_acts (
    id          bigserial primary key,
    carrier     text          not null check (carrier in ('cdek', 'ozon')),
    track       text          not null,
    amount      numeric(12,2) not null check (amount >= 0),
    kind        text          not null default 'доставка' check (kind in ('доставка', 'возврат')),
    act_date    date,
    act_number  text,
    entered_by  text          not null check (fin.valid_author(entered_by)),
    created_at  timestamptz   not null default now()
);
create index if not exists carrier_acts_track_idx on fin.carrier_acts (track);
comment on table fin.carrier_acts is
    'Акты перевозчиков: фактическая стоимость по трек-номеру. Последняя строка по (трек, вид) действует.';

-- --- функции ----------------------------------------------------------------------

-- A8: эквайринг способа оплаты × (1 + НДС на комиссию) + фискализация; способа нет — карты.
create or replace function fin.payment_rate_for(method text, on_date date default current_date) returns numeric
    language sql stable as $$
    select coalesce(fin.inp('acquiring_rate_' || coalesce(method, ''), on_date), fin.inp('acquiring_rate', on_date))
           * (1 + coalesce(fin.inp('vat_on_fee', on_date), 0))
         + coalesce(fin.inp('fiscal_rate', on_date), 0) * coalesce(fin.inp('fiscal_units', on_date), 1)
$$;

-- КУДиР бота — через представление fin: функции fin выполняются с правами
-- спрашивающего (metabase_ro), а finance.v_yk_kudir ему не выдан.
create or replace view fin.v_kudir as
select income_date, doc_number, doc_date, content, income, kind from finance.v_yk_kudir;
comment on view fin.v_kudir is 'КУДиР (доходы УСН) по реестру ЮKassa и выписке; дата дохода — правило бота KUDIR_INCOME_DATE.';

create or replace view fin.v_contributions_paid as
select done_on, kind, paid_amount from fin.tax_calendar where status = 'оплачено' and kind in ('взносы', 'взносы_1%');

-- A6: налог УСН с начала года на дату — 6 % от доходов КУДиР минус уплаченные с
-- начала года взносы, не меньше нуля (ИП без работников уменьшает налог на взносы целиком).
create or replace function fin.usn_tax_ytd(d date) returns numeric
    language sql stable as $$
    select greatest(0,
        fin.inp('usn_rate', d) * coalesce((select sum(income) from fin.v_kudir
                                           where income_date between date_trunc('year', d)::date and d), 0)
        - coalesce((select sum(paid_amount) from fin.v_contributions_paid
                    where done_on between date_trunc('year', d)::date and d), 0))
$$;

-- Резерв УСН за [s, e): прирост налога с начала года; через Новый год — по частям.
create or replace function fin.usn_reserve_between(s date, e date) returns numeric
    language sql stable as $$
    select case
        when e <= s then 0
        when extract(year from s) = extract(year from e - 1)
            then fin.usn_tax_ytd(e - 1)
                 - case when extract(year from s - 1) = extract(year from s) then fin.usn_tax_ytd(s - 1) else 0 end
        else fin.usn_tax_ytd(make_date(extract(year from s)::int, 12, 31))
             - case when extract(year from s - 1) = extract(year from s) then fin.usn_tax_ytd(s - 1) else 0 end
             + fin.usn_tax_ytd(e - 1)
    end
$$;

-- Следующий рабочий день (пн–пт; праздники не учитываются).
create or replace function fin.next_business_day(d date) returns date
    language sql immutable as $$
    select d + case extract(isodow from d)::int when 5 then 3 when 6 then 2 else 1 end
$$;

-- --- A5 / A8 / A13: экономика заказа ---------------------------------------------
-- Выручка — брутто (сколько заплатил покупатель). Эквайринг — комиссия ЮKassa из
-- реестра (amount − income_amount) плюс фискализация по ставке; пока платежа в
-- реестре нет — ставка способа оплаты. Доставка — акт или факт API, иначе котировка.
create or replace view fin.v_order_economics as
with o as (
    select v.order_id, v.client_key, v.paid_date_msk, v.is_refunded, v.free_delivery,
           v.client_paid_order_number = 1 as is_first_order,
           v.total, v.items_total, coalesce(v.delivery_cost, 0) as delivery_paid_by_client,
           v.carrier_delivery_cost, v.payment_method, v.carrier_actual_cost, v.carrier_track
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
           l.packs, l.units, coalesce(l.packs, 0) >= 2 as two_plus_packs
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
       c.payment_method, c.acquiring_source
from c;
comment on view fin.v_order_economics is
    'Экономика оплаченного заказа: выручка брутто, себестоимость, упаковка, доставка (акт / API / котировка), обратная доставка, эквайринг (факт ЮKassa или ставка способа), резерв УСН, Claude API, маржа до CAC.';

-- --- A6: P&L без двойного учёта налога и взносов --------------------------------
create or replace view fin.v_pnl_base as
with m as (select * from fin.v_pilot_months),
orders as (
    select m.month_no, count(*) as orders, count(*) filter (where e.is_first_order) as new_clients,
           sum(e.revenue) as revenue, sum(e.cogs_tea) as cogs_tea, sum(e.packaging_and_codes) as packaging_and_codes,
           sum(e.delivery_subsidy) as delivery_subsidy, sum(e.revenue) filter (where e.is_refunded) as returns,
           sum(e.acquiring) as acquiring,
           count(*) filter (where e.is_first_order) as first_orders,
           sum(e.margin_before_cac + e.usn_reserve) filter (where e.is_first_order) as first_orders_margin_before_usn,
           sum(e.carrier_return_cost) as carrier_returns
    from fin.v_order_economics e
    join m on e.paid_date_msk >= m.period_start and e.paid_date_msk < m.period_end
    group by m.month_no
),
ads as (
    select m.month_no, sum(a.spend_own) as ads, sum(a.spend_bonus) as ads_bonus
    from fin.ad_spend a join m on a.date >= m.period_start and a.date < m.period_end group by m.month_no
),
bank as (
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
    select m.month_no, sum(c.amount_usd) * fin.inp('usd_rub') as claude_api
    from analytics.v_anthropic_costs c join m on c.day >= m.period_start and c.day < m.period_end group by m.month_no
),
p as (
    select m.month_no, m.month_label, m.period_start, m.period_end, m.period_start <= current_date as started,
           o.orders, o.new_clients, o.revenue, o.cogs_tea, o.packaging_and_codes, o.delivery_subsidy, o.returns,
           o.acquiring, o.first_orders, o.first_orders_margin_before_usn, o.carrier_returns,
           a.ads, a.ads_bonus,
           coalesce(b.contractor, 0) + coalesce(x.contractor, 0) as contractor,
           coalesce(b.other_opex, 0) + coalesce(x.other_opex, 0) + coalesce(c.claude_api, 0) as other_opex,
           c.claude_api,
           -- Резерв УСН месяца — прирост налога с начала года (КУДиР − уплаченные взносы);
           -- текущий месяц — по сегодня.
           fin.usn_reserve_between(m.period_start, least(m.period_end, current_date + 1)) as usn_reserve,
           (select sum(income) from fin.v_kudir k where k.income_date >= m.period_start and k.income_date < m.period_end)
               as usn_income,
           (select sum(paid_amount) from fin.v_contributions_paid w
             where w.done_on >= m.period_start and w.done_on < m.period_end) as contributions_paid
    from m left join orders o using (month_no) left join ads a using (month_no) left join bank b using (month_no)
    left join manual x using (month_no) left join api c using (month_no)
)
select p.month_no, p.month_label, p.period_start, p.period_end,
       case when p.started then coalesce(p.orders, 0) end as orders,
       case when p.started then coalesce(p.new_clients, 0) end as new_clients,
       case when p.started then coalesce(p.revenue, 0) end as revenue,
       case when p.started then coalesce(p.cogs_tea, 0) end as cogs_tea,
       case when p.started then coalesce(p.packaging_and_codes, 0) end as packaging_and_codes,
       case when p.started then coalesce(p.delivery_subsidy, 0) + coalesce(p.carrier_returns, 0) end as delivery_subsidy,
       case when p.started then coalesce(p.returns, 0) end as returns,
       case when p.started then coalesce(p.acquiring, 0) end as acquiring,
       case when p.started then coalesce(p.revenue, 0) - coalesce(p.cogs_tea, 0) - coalesce(p.packaging_and_codes, 0)
           - coalesce(p.delivery_subsidy, 0) - coalesce(p.carrier_returns, 0) - coalesce(p.returns, 0)
           - coalesce(p.acquiring, 0) end as margin_before_cac,
       case when p.started then coalesce(p.ads, 0) end as ads,
       case when p.started then coalesce(p.ads_bonus, 0) end as ads_bonus,
       case when p.started then p.contractor end as contractor,
       case when p.started then p.other_opex end as other_opex,
       case when p.started then p.claude_api end as claude_api,
       case when p.started then p.usn_reserve end as usn_reserve,
       -- Взносы — справочно (год / 12): в прибыль не входят, это деньги (fin.v_cash_forecast),
       -- а налог уже уменьшен на уплаченные взносы.
       case when p.started and p.month_no > 0 then
           case extract(year from p.period_start) when 2026 then fin.inp('ip_fixed_contrib_2026')
                                                  when 2027 then fin.inp('ip_fixed_contrib_2027') end / 12 end as contributions_reserve,
       case when p.started then
           coalesce(p.revenue, 0) - coalesce(p.cogs_tea, 0) - coalesce(p.packaging_and_codes, 0) - coalesce(p.delivery_subsidy, 0)
           - coalesce(p.carrier_returns, 0) - coalesce(p.returns, 0) - coalesce(p.acquiring, 0) - coalesce(p.ads, 0)
           - p.contractor - p.other_opex - coalesce(p.usn_reserve, 0)
       end as operating_profit,
       p.revenue / nullif(p.orders, 0) as avg_check,
       (coalesce(p.ads, 0) + p.contractor) / nullif(p.new_clients, 0) as cac,
       p.first_orders_margin_before_usn / nullif(p.first_orders, 0)
           - (coalesce(p.ads, 0) + p.contractor) / nullif(p.new_clients, 0) as first_order_after_cac,
       -- новые столбцы (006)
       case when p.started then coalesce(p.usn_income, 0) end as usn_income,
       case when p.started then coalesce(p.contributions_paid, 0) end as contributions_paid
from p;
comment on view fin.v_pnl_base is
    'P&L по месяцам пилота. Резерв УСН = прирост max(0; 6 % × доходы КУДиР с начала года − уплаченные взносы); взносы — деньгами (v_cash_forecast), в прибыль не входят.';

create or replace view fin.v_pnl_monthly as
select p.month_no, p.month_label, p.period_start, p.period_end, p.orders, p.new_clients, p.revenue, p.cogs_tea,
       p.packaging_and_codes, p.delivery_subsidy, p.returns, p.acquiring, p.margin_before_cac, p.ads, p.ads_bonus,
       p.contractor, p.other_opex, p.claude_api, p.usn_reserve, p.contributions_reserve, p.operating_profit,
       p.avg_check, p.cac, p.first_order_after_cac,
       p.operating_profit / nullif(p.revenue, 0) as operating_margin,
       sum(p.operating_profit) filter (where p.month_no between 1 and coalesce(fin.inp('pilot_months')::int, 3))
           over (order by p.month_no) as pilot_cumulative_profit,
       p.usn_income, p.contributions_paid
from fin.v_pnl_base p;

-- --- A1: платный CAC ----------------------------------------------------------------
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
       fe.dialogs_started, fe.drafts, fe.invoices, f.new_clients as first_paid,
       -- новые столбцы (006): платный CAC — реклама своими / новые клиенты из рекламы (first touch ad:*).
       f.new_clients_ad,
       coalesce(a.spend_own, 0) / nullif(f.new_clients_ad, 0) as cac_paid,
       f.first_order_margin_ad - coalesce(a.spend_own, 0) / nullif(f.new_clients_ad, 0) as first_order_after_cac_paid
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
    select count(*) as new_clients, avg(e.margin_before_cac + e.usn_reserve) as first_order_margin,
           count(*) filter (where c.first_source_kind = 'ad') as new_clients_ad,
           avg(e.margin_before_cac + e.usn_reserve) filter (where c.first_source_kind = 'ad') as first_order_margin_ad
    from fin.v_order_economics e left join analytics.v_clients c using (client_key)
    where e.is_first_order and e.paid_date_msk >= p.period_start and e.paid_date_msk < p.period_end
    having count(*) > 0
) f on true
left join lateral (
    select count(*) filter (where event = 'dialog_start') as dialogs_started,
           count(*) filter (where event = 'draft_created') as drafts,
           count(*) filter (where event like 'invoice\_%' and event <> 'invoice_expired') as invoices
    from analytics.v_funnel_events where created_date_msk >= p.period_start and created_date_msk < p.period_end
) fe on true;

-- --- A9: безубыточность по фактической марже ------------------------------------
create or replace view fin.v_breakeven as
with lead as (
    select u.product, u.margin_before_cac from fin.v_unit_cost u
    where u.product = (select value_text from fin.inputs_current where key = 'lead_product')
),
fact as (
    select count(*) as orders, avg(margin_before_cac) as margin
    from fin.v_order_economics
    where margin_before_cac is not null
      and paid_date_msk > current_date - coalesce(fin.inp('breakeven_fact_days')::int, 14)
),
basis as (
    select f.orders as fact_orders, f.margin as fact_margin,
           f.orders >= coalesce(fin.inp('breakeven_fact_min_orders'), 10) and f.margin is not null as use_fact
    from fact f
),
plan_ads as (
    select distinct on (month) month, value from fin.plan where metric = 'ads' order by month, created_at desc
),
r as (
    select m.month_no, m.month_label, m.period_start, l.product as lead_product, l.margin_before_cac as lead_margin,
           fin.fixed_month(m.period_start) as fixed_costs,
           coalesce(pa.value, case when m.month_no = 1 then fin.inp('first_ad_test_own') end) as acquisition_ads,
           fin.contractor_monthly() as contractor,
           b.fact_orders, b.fact_margin, b.use_fact,
           case when b.use_fact then b.fact_margin else l.margin_before_cac end as margin_used
    from fin.v_pilot_months m
    cross join lead l cross join basis b
    left join plan_ads pa on pa.month = m.month_no
    where m.month_no >= 1
)
select r.month_no, r.month_label, r.period_start, r.lead_product, r.lead_margin, r.fixed_costs, r.acquisition_ads,
       r.contractor,
       case when r.margin_used > 0 and r.acquisition_ads is not null
            then ceil((r.acquisition_ads + r.contractor + r.fixed_costs) / r.margin_used) end as orders_to_break_even,
       -- новые столбцы (006)
       case when r.use_fact then 'факт: средняя маржа заказа до CAC за '
                                 || coalesce(fin.inp('breakeven_fact_days')::int, 14) || ' дней'
            else 'маржа «' || r.lead_product || '» (оплаченных заказов за окно: ' || coalesce(r.fact_orders, 0) || ')' end
           as margin_basis,
       r.margin_used, r.fact_orders, r.fact_margin
from r;
comment on view fin.v_breakeven is
    'Заказов для безубыточности месяца = (реклама + подрядчик + постоянные) / маржа: фактическая средняя за 14 дней при ≥ 10 оплатах, иначе маржа lead-продукта. margin_basis — какая взята.';

-- --- A10: точка заказа ---------------------------------------------------------------
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
       -- Товара нет в каталоге бота (не продаётся в диалоге) — дни запаса всё равно считаем.
       coalesce((select s.bot_name from fin.sku s where s.sku_name = b.product),
                (select x.bot_name from fin.bundles x where x.bundle = b.product)) is null as not_in_bot
from base b;
comment on view fin.v_stock is
    'Склад: граммы, пачки и сеты на руках, дни запаса, точка заказа = (срок поставки + фасовка + страховой запас) × продажи в день за 14 дней; order_now — пора заказывать. Пусто — нет данных (вводные supply_lead_days, packing_marking_days, safety_stock_days).';

-- --- A5: «ЮKassa в пути» из реестра -------------------------------------------------
create or replace view fin.v_yk_in_transit as
select (select coalesce(sum(income_amount), 0) from finance.yk_payments
         where status = 'succeeded' and payout_date is null and income_amount is not null)
     - (select coalesce(sum(amount), 0) from finance.yk_refunds where status = 'succeeded' and payout_date is null)
       as amount,
       (select max(synced_at) from finance.yk_payments) as registry_synced_at;
comment on view fin.v_yk_in_transit is 'ЮKassa в пути: оплачено покупателями, но ещё не выплачено на счёт (реестр бота минус возвраты).';

create or replace view fin.v_cash_status as
with last_day as (select max(day) as day from finance.v_daily_balance_total),
pnl as (
    select count(*) filter (where operating_profit is not null) as months_with_fact,
           sum(operating_profit) as profit
    from fin.v_pnl_base where month_no between 1 and coalesce(fin.inp('pilot_months')::int, 3)
),
transit as (
    -- Реестр ЮKassa есть — из него; нет — как раньше, по заказам после последнего дня выписки.
    select case when exists (select 1 from finance.yk_payments) then (select amount from fin.v_yk_in_transit)
                else (select sum(e.revenue - e.acquiring) from fin.v_order_economics e cross join last_day l
                      where e.paid_date_msk > l.day) end as amount
),
snap as (select date, balance from fin.cash_snapshots order by date desc, created_at desc limit 1),
holds as (
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

-- --- A11: деньги на 8 недель --------------------------------------------------------
create or replace view fin.v_cash_forecast_daily as
with start as (
    select (select balance from finance.v_daily_balance_total order by day desc limit 1)
           - coalesce((select sum(holds_out) from finance.v_pending_summary), 0) as balance
),
days as (select d::date as day from generate_series(current_date, current_date + 55, interval '1 day') d),
transit as (
    -- Нетто ЮKassa по дню оплаты приходит на счёт следующим рабочим днём; опоздавшее — завтра.
    select greatest(fin.next_business_day(d), current_date + 1) as day, sum(v) as amount from (
        select (captured_at at time zone 'Europe/Moscow')::date as d, income_amount as v from finance.yk_payments
        where status = 'succeeded' and payout_date is null and income_amount is not null
        union all
        select (created_at at time zone 'Europe/Moscow')::date, -amount from finance.yk_refunds
        where status = 'succeeded' and payout_date is null
    ) x group by 1
),
plan as (select date as day, sum(amount) as amount from fin.expenses where is_plan group by date),
taxes as (
    select due_date as day, sum(amount_hint) as amount from fin.tax_calendar
    where status = 'ожидает' and kind in ('взносы', 'взносы_1%', 'налог', 'аванс') group by due_date
),
flows as (
    select d.day,
           coalesce(t.amount, 0) as payouts_in,
           coalesce(pl.amount, 0) as planned_expenses,
           case when d.day >= coalesce(fin.inp_date('ad_campaign_start'), fin.inp_date('pilot_start_date'))
                 and d.day <= coalesce(fin.inp_date('ad_campaign_end'), 'infinity'::date)
                then coalesce(fin.inp('ad_daily_budget'), 0) else 0 end as ads,
           coalesce(tx.amount, 0) as taxes,
           case when extract(day from d.day) = 1 and d.day >= fin.inp_date('bank_paid_from')
                then coalesce(fin.inp('bank_fee_monthly'), 0) else 0 end as bank_fee,
           -- Сервисы (Timeweb + Aeza) списываются понемногу каждый день.
           coalesce(fin.inp('infra_monthly'), 0) * 12 / 365.0 as services
    from days d
    left join transit t on t.day = d.day
    left join plan pl on pl.day = d.day
    left join taxes tx on tx.day = d.day
)
select f.*,
       s.balance + sum(f.payouts_in - f.planned_expenses - f.ads - f.taxes - f.bank_fee - f.services)
           over (order by f.day) as closing
from flows f cross join start s;

create or replace view fin.v_cash_forecast as
with w as (
    select ((day - current_date) / 7) + 1 as week_no, min(day) as week_start, max(day) as week_end,
           sum(payouts_in) as payouts_in, sum(planned_expenses) as planned_expenses, sum(ads) as ads, sum(taxes) as taxes,
           sum(bank_fee) as bank_fee, sum(services) as services,
           (array_agg(closing order by day desc))[1] as closing,
           min(closing) as lowest
    from fin.v_cash_forecast_daily group by 1
),
breach as (
    select min(day) as day from fin.v_cash_forecast_daily where closing < fin.inp('min_cash_reserve')
)
select w.week_no, w.week_start, w.week_end,
       w.closing - (w.payouts_in - w.planned_expenses - w.ads - w.taxes - w.bank_fee - w.services) as opening,
       w.payouts_in, w.planned_expenses, w.ads, w.taxes, w.bank_fee, w.services, w.closing, w.lowest,
       fin.inp('min_cash_reserve') as min_cash_reserve,
       w.lowest < fin.inp('min_cash_reserve') as below_reserve,
       b.day as reserve_breach_date,
       fin.inp('ad_daily_budget') is null as ads_plan_missing
from w cross join breach b
order by w.week_no;
comment on view fin.v_cash_forecast is
    'Деньги на 8 недель: остаток по выписке минус холды + выплаты ЮKassa в пути − плановые расходы − реклама по плану − налоги и взносы по сроку − банк − сервисы. reserve_breach_date — первый день ниже минимального резерва. ads_plan_missing — дневной бюджет рекламы не задан.';

-- --- A12: ворота пилота ----------------------------------------------------------------
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
checks (sort, gate, check_name, value, threshold, higher_is_better, period_end, unit) as (
    select 1, 'Неделя 2', 'Конверсия платного диалога в оплату',
           w2.paid::numeric / nullif(w2.dialogs, 0), fin.inp('gate_w2_conv_min'), true, s.start + 14, '%'
    from w2 cross join s
    union all
    select 2, 'Неделя 6', 'Платный CAC', w6.spend / nullif(w6.new_clients, 0), fin.inp('gate_w6_cac_max'), false,
           s.start + 42, '₽' from w6 cross join s
    union all
    select 3, 'Неделя 6', 'Маржа первого заказа после CAC',
           w6.first_margin - w6.spend / nullif(w6.new_clients, 0), fin.inp('gate_w6_first_margin_min'), true,
           s.start + 42, '₽' from w6 cross join s
    union all
    select 4, 'М3', 'Повтор за 45 дней у первой когорты',
           (select avg(case when repeat45 then 1 else 0 end) from cohort), fin.inp('gate_m3_repeat45_target'), true,
           (select period_end from fin.v_pilot_months where month_no = 3), '%' from s
    union all
    select 5, 'М3', 'Прогноз: маржа за 90 дней − CAC',
           (select avg(margin90) from cohort) - (select cac from m1_cac), fin.inp('gate_m3_ltv90_min'), true,
           (select period_end from fin.v_pilot_months where month_no = 3), '₽' from s
    union all
    select 6, 'М3', 'Операционная прибыль М3',
           (select operating_profit from fin.v_pnl_monthly where month_no = 3), fin.inp('gate_m3_profit_min'), true,
           (select period_end from fin.v_pilot_months where month_no = 3), '₽' from s
)
select gate, check_name, value, threshold, unit,
       case when value is null or threshold is null then 'нет данных'
            when (higher_is_better and value >= threshold) or (not higher_is_better and value <= threshold) then 'зелёный'
            else 'красный' end as status,
       period_end, current_date >= period_end as period_complete, sort
from checks order by sort;
comment on view fin.v_pilot_gates is
    'Ворота пилота: неделя 2 (конверсия платного диалога ≥ 15 %), неделя 6 (платный CAC ≤ 700 ₽, маржа первого заказа после CAC ≥ 0), М3 (повтор 45 дней, маржа 90 дней − CAC, прибыль М3). Пороги — вводные gate_*. Нет значения или порога — «нет данных».';

-- --- A13: сверка доставки с актами и API ---------------------------------------------
create or replace view fin.v_carrier_check as
select e.order_id, e.paid_date_msk, date_trunc('month', e.paid_date_msk)::date as month,
       coalesce(v.carrier, (select a.carrier from fin.carrier_acts a where a.track = v.carrier_track limit 1),
                case when v.carrier_actual_cost is not null then 'cdek' end) as carrier,
       e.carrier_quote, e.carrier_actual, e.carrier_return_cost, e.carrier_cost_source,
       (e.carrier_actual - e.carrier_quote) / nullif(e.carrier_quote, 0) as deviation,
       abs(e.carrier_actual - e.carrier_quote) / nullif(e.carrier_quote, 0)
           > coalesce(fin.inp('carrier_deviation_max'), 0.10) as over_limit
from fin.v_order_economics e join analytics.v_orders v using (order_id)
where e.carrier_actual is not null;
comment on view fin.v_carrier_check is 'Доставка: котировка против факта (акт или API) по заказу; over_limit — отклонение больше carrier_deviation_max.';

create or replace view fin.v_carrier_check_monthly as
select month, carrier, count(*) as orders, sum(carrier_quote) as quote, sum(carrier_actual) as actual,
       sum(carrier_return_cost) as returns,
       (sum(carrier_actual) - sum(carrier_quote)) / nullif(sum(carrier_quote), 0) as deviation,
       abs(sum(carrier_actual) - sum(carrier_quote)) / nullif(sum(carrier_quote), 0)
           > coalesce(fin.inp('carrier_deviation_max'), 0.10) as over_limit
from fin.v_carrier_check group by month, carrier;

-- --- «Последние записи»: без строк миграции, с новыми формами ------------------------
create or replace view fin.v_recent_entries as
select * from (
    select 'Вводная' as form, created_at, entered_by,
           coalesce(label, key) || ': ' ||
           case when value is null then coalesce(value_text, 'нет данных')
                when unit = '%' then fin.fmt_pct(value)
                else trim_scale(value)::text || coalesce(' ' || nullif(unit, ''), '') end as what, note
    from fin.inputs where entered_by not in ('импорт v1.5', 'миграция')
    union all select 'Расход', created_at, entered_by, category || ': ' || fin.fmt_rub(amount) || ' ' || coalesce(description, ''),
                     case when is_plan then 'план' else paid_from end from fin.expenses
    union all select 'Вклад / выплата', created_at, entered_by, partner || ' ' || kind || ' ' || fin.fmt_rub(amount), note from fin.capital
    union all select 'Остаток по выписке', created_at, entered_by, to_char(date, 'DD.MM.YYYY') || ': ' || fin.fmt_rub(balance), source
              from fin.cash_snapshots
    union all select 'Партия закупки', created_at, entered_by,
                     sku_name || ': ' || qty_units::float8 || ' × ' || fin.fmt_rub(price_per_unit), supplier from fin.purchase_lots
    union all select 'Фасовка', created_at, entered_by, product || ': ' || packs_out || ' шт', note from fin.packing_runs
    union all select 'Реклама за день', created_at, entered_by,
                     to_char(date, 'DD.MM.YYYY') || ' ' || channel || ': ' || fin.fmt_rub(spend_own) || ' своих', note from fin.ad_spend
    union all select 'Акт перевозчика', created_at, entered_by, carrier || ' ' || track || ': ' || fin.fmt_rub(amount), kind
              from fin.carrier_acts
    union all select 'Срок поставки', created_at, entered_by, scope || ' ' || name || ': ' || lead_days || ' дн.', note
              from fin.supply_lead_times
) e order by created_at desc limit 20;

insert into fin.schema_migrations (version) values ('006_pilot_controls') on conflict do nothing;
