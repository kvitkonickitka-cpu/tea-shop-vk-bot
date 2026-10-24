-- 013: плановые платежи в прогнозе и бюджете, статья «Декларация о соответствии», FCF.
--
-- До этой миграции плановый расход формы «Добавить расход» (fin.expenses.is_plan) видел
-- только fin.v_cash_forecast_daily, на котором нет ни одной карточки: декларация на
-- 33 000 ₽ к 05.11.2026 не попадала ни в прогноз «Денег хватит до», ни в бюджет пилота.
--
-- Теперь:
-- - у расхода есть статья бюджета (budget_item; пусто — по категории и описанию, как раньше);
-- - открытый план — в прогнозе на свою дату (просроченный — на завтра) и в бюджете
--   своей статьи (столбец planned; «осталось» — без него, чтобы не списать дважды);
-- - план закрывает факт: расход «мимо счёта» или со счёта (closes_plan_id) либо
--   разметка операции выписки (fin.bank_marks.closes_plan_id). Закрытый план из
--   прогноза уходит, деньги считаются по факту;
-- - просроченный и не закрытый план — строка в «Требует внимания»;
-- - FCF по месяцам и неделям: fin.cash_flows(grain), представления v_fcf_monthly, v_fcf_weekly.

-- --- статья и вводная декларации ---------------------------------------------------------------
insert into fin.budget_articles (item, line_no, kind, cash_class, color, sort)
values ('Декларация о соответствии', 16, 'разовая', 'Операционные', '#C46A6A', 16)
on conflict (item) do nothing;

insert into fin.inputs (key, label, value, unit, status, note, valid_from, entered_by)
select 'declaration_cost', 'Декларация о соответствии ТР ТС 021', 33000, '₽', 'Допущение',
       'Верхняя оценка: испытания в аккредитованной лаборатории + пошлина 1 500 ₽', '2026-10-24', 'миграция'
where not exists (select 1 from fin.inputs where key = 'declaration_cost');

insert into fin.budget_article_map (source, match_key, match_text, budget_item, priority, note)
select 'мимо счёта', 'прочее', '%декларац%', 'Декларация о соответствии', 10, 'Декларация, испытания, пошлина'
where not exists (select 1 from fin.budget_article_map where budget_item = 'Декларация о соответствии');

-- --- статья расхода и закрытие плана -----------------------------------------------------------
alter table fin.expenses add column if not exists budget_item text
    references fin.budget_articles (item) on update cascade;
alter table fin.expenses add column if not exists closes_plan_id bigint references fin.expenses (id);
alter table fin.bank_marks add column if not exists closes_plan_id bigint references fin.expenses (id);
comment on column fin.expenses.budget_item is 'Статья бюджета пилота. Пусто — по категории и описанию (fin.budget_article_map).';
comment on column fin.expenses.closes_plan_id is 'Этот факт оплатил плановый расход №… — план уходит из прогноза.';
comment on column fin.bank_marks.closes_plan_id is 'Операция выписки оплатила плановый расход №… — план уходит из прогноза.';

-- Закрыть можно только план, и план не закрывает сам себя.
create or replace function fin.check_closes_plan() returns trigger language plpgsql as $$
begin
    if new.closes_plan_id is null then
        return new;
    end if;
    -- Вложенно: у fin.bank_marks поля is_plan нет, и условие с ним не должно вычисляться.
    if tg_table_name = 'expenses' then
        if new.is_plan then
            raise exception 'Плановый расход не закрывает другой план — закрывает факт';
        end if;
    end if;
    if not exists (select 1 from fin.expenses where id = new.closes_plan_id and is_plan) then
        raise exception 'Расход №% — не план, закрыть его нельзя', new.closes_plan_id;
    end if;
    return new;
end $$;
drop trigger if exists expenses_closes_plan on fin.expenses;
create trigger expenses_closes_plan before insert or update on fin.expenses
    for each row execute function fin.check_closes_plan();
drop trigger if exists bank_marks_closes_plan on fin.bank_marks;
create trigger bank_marks_closes_plan before insert or update on fin.bank_marks
    for each row execute function fin.check_closes_plan();

-- Строка id 8 на бою — декларация, 33 000 ₽, план на 05.11.2026, категория «прочее».
update fin.expenses set budget_item = 'Декларация о соответствии'
 where id = 8 and is_plan and amount = 33000 and budget_item is null;

-- Статья и вид денег каждого расхода — одно правило для факта и для плана: своя статья;
-- у факта, закрывающего план, — статья плана; иначе — по категории и описанию.
create or replace view fin.v_expense_items as
with base as (
    select x.id, x.category, x.closes_plan_id, x.budget_item as own, mm.budget_item as mapped
    from fin.expenses x
    left join lateral (
        select m.budget_item from fin.budget_article_map m
        where m.source = 'мимо счёта' and m.match_key = x.category
          -- Регистр — переводом, а не ILIKE: у ILIKE кириллица зависит от локали базы.
          and (m.match_text is null
               or translate(coalesce(x.description, ''), 'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ',
                            'абвгдеёжзийклмнопрстуфхцчшщъыьэюя') like m.match_text)
        order by m.priority, m.id limit 1
    ) mm on true
),
item as (
    select b.id, b.category, coalesce(b.own, p.own, p.mapped, b.mapped, 'Не сопоставлено') as budget_item
    from base b left join base p on p.id = b.closes_plan_id
)
select i.id, i.budget_item,
       coalesce(ba.cash_class, case when i.category = 'оборудование' then 'Вложения' else 'Операционные' end) as cash_class
from item i
left join fin.budget_articles ba on ba.item = i.budget_item;
comment on view fin.v_expense_items is 'Статья бюджета и вид денег расхода: своя статья, иначе — по категории и описанию.';

-- Плановые платежи: открыт ли, чем закрыт, на какой день стоит в прогнозе.
create or replace view fin.v_plans as
with marks as (
    select operation_id,
           (array_agg(closes_plan_id order by created_at desc) filter (where closes_plan_id is not null))[1] as plan_id
    from fin.bank_marks group by operation_id
),
closers as (
    select x.closes_plan_id as plan_id, x.date as closed_on, 'расход №' || x.id as closed_by
    from fin.expenses x where x.closes_plan_id is not null and not x.is_plan
    union all
    select m.plan_id, o.day, 'операция выписки ' || left(m.operation_id, 8)
    from marks m
    left join (select operation_id, min(day) as day from fin.v_bank_ops group by 1) o using (operation_id)
    where m.plan_id is not null
)
select x.id, x.date as plan_date, x.amount, coalesce(nullif(x.description, ''), x.category) as what, x.category,
       e.budget_item, e.cash_class, x.paid_from,
       c.plan_id is not null as closed, c.closed_on, c.closed_by,
       c.plan_id is null and x.date < current_date as overdue,
       case when c.plan_id is null then greatest(x.date, current_date + 1) end as due_day
from fin.expenses x
join fin.v_expense_items e on e.id = x.id
left join lateral (select * from closers c where c.plan_id = x.id order by c.closed_on limit 1) c on true
where x.is_plan;
comment on view fin.v_plans is
    'Плановые расходы: статья, закрыт ли фактом (расход или операция выписки), просрочен ли; due_day — день в прогнозе.';

-- Расход «мимо счёта» — со своей статьёй, если она указана (fin.v_expense_items).
create or replace view fin.v_spend as
select 'выписка'::text as source, b.day, b.amount, b.budget_item, b.cash_class, b.article as what, b.operation_id::text as ref
from fin.v_bank_ops b
where b.direction = 'out' and b.cash_class in ('Операционные', 'Вложения')
union all
select 'мимо счёта', x.date, x.amount, e.budget_item, e.cash_class, x.category, 'expense:' || x.id
from fin.expenses x
join fin.v_expense_items e on e.id = x.id
where not x.is_plan and x.paid_from = 'мимо счёта';

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
    -- 013: декларация о соответствии ТР ТС 021 — нас как изготовителя (упаковщика).
    union all select 16, 'Декларация о соответствии', fin.inp('declaration_cost'), fin.inp('declaration_cost'),
                         'вводная declaration_cost: испытания + пошлина, верхняя оценка'
)
select line_no, item, plan_amount, max_amount, source,
       sum(plan_amount) over () as known_total,
       fin.inp('capital_plan') as capital_plan,
       fin.inp('capital_plan') - sum(plan_amount) over () as left_before_unknown
from lines;

create or replace view fin.v_pilot_budget_status as
with spent as (
    select budget_item, sum(amount) as spent,
           sum(amount) filter (where day < fin.inp_date('pilot_start_date')) as before_start,
           sum(amount) filter (where day >= fin.inp_date('pilot_start_date')) as in_pilot
    from fin.v_spend
    where day >= fin.inp_date('ip_registration_date')
      and day < coalesce(fin.pilot_month_start(coalesce(fin.inp('pilot_months')::int, 3) + 1), 'infinity'::date)
    group by 1
),
-- 013: открытые плановые платежи — уже назначенная часть статьи; «осталось» без них,
-- чтобы прогноз не списал одни и те же деньги дважды: план на свою дату и остаток статьи.
planned as (
    select budget_item, sum(amount) as planned from fin.v_plans where not closed group by 1
)
select b.line_no, b.item, ba.kind, b.plan_amount as plan, coalesce(s.spent, 0) as spent,
       case when b.plan_amount is not null then b.plan_amount - coalesce(s.spent, 0) - coalesce(p.planned, 0) end as left_amount,
       coalesce(s.spent, 0) / nullif(b.plan_amount, 0) as spent_share, ba.sort, ba.color,
       -- новые столбцы (010)
       coalesce(s.before_start, 0) as spent_before_start, coalesce(s.in_pilot, 0) as spent_in_pilot,
       -- новый столбец (013)
       coalesce(p.planned, 0) as planned
from fin.v_pilot_budget b
join fin.budget_articles ba on ba.item = b.item
left join spent s on s.budget_item = b.item
left join planned p on p.budget_item = b.item
union all
select null, 'Не сопоставлено', 'нет', null, coalesce(s.spent, 0), null, null, 99, '#9A9996',
       coalesce(s.before_start, 0), coalesce(s.in_pilot, 0), coalesce(p.planned, 0)
from (select 'Не сопоставлено'::text as item) n
left join spent s on s.budget_item = n.item
left join planned p on p.budget_item = n.item
where coalesce(s.spent, 0) > 0 or coalesce(p.planned, 0) > 0;



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
h as (
    select greatest(s.pilot_end + 30, current_date + 56) as horizon,
           case when c.amount > 0 then c.day else current_date + 1 end as one_off_from,
           greatest(s.pilot_start - 1, case when c.amount > 0 then c.day else current_date + 1 end + 6) as one_off_until,
           greatest(coalesce(fin.inp_date('tea_purchase_date'), s.pilot_start - 14), current_date + 1) as tea_day,
           greatest(coalesce(fin.inp_date('vk_topup_date'), s.pilot_start - 7), current_date + 1) as vk_day
    from s cross join capital c
),
days as (select d::date as day from h, generate_series(current_date + 1, h.horizon, interval '1 day') d),
left_by as (
    select coalesce(sum(greatest(coalesce(left_amount, 0), 0)) filter (where item in ('Первая закупка чая', 'Доставка основной партии')), 0) as tea,
           coalesce(sum(greatest(coalesce(left_amount, 0), 0)) filter (where item = 'ВК: пополнение кабинета'), 0) as vk,
           coalesce(sum(greatest(coalesce(left_amount, 0), 0)) filter (where kind = 'разовая'
               and item not in ('Первая закупка чая', 'Доставка основной партии')), 0) as other
    from fin.v_pilot_budget_status
),
months as (
    select n, fin.pilot_month_start(n) as m_start from generate_series(1, coalesce(fin.inp('pilot_months')::int, 3)) n
),
-- 013: открытые плановые платежи формы «Добавить расход» — на свои даты. Просроченный
-- и не закрытый фактом — на завтра: пока факт не привязан, считаем, что не оплачен.
plans as (
    select due_day as day, sum(amount) as amount from fin.v_plans where not closed group by 1
),
taxes as (
    select due_date as day, sum(amount_hint) as amount from fin.tax_calendar
    where status = 'ожидает' and kind in ('взносы', 'взносы_1%', 'налог', 'аванс') group by due_date
),
flows as (
    select d.day,
           case when d.day = c.day then c.amount else 0 end as capital_in,
           coalesce(y.amount, 0) as payouts_in,
           case when d.day between h.one_off_from and h.one_off_until
                then l.other / (h.one_off_until - h.one_off_from + 1) else 0 end
             + case when d.day = h.tea_day then l.tea else 0 end as budget_out,
           case when d.day = h.vk_day then l.vk else 0 end as ads_out,
           coalesce(fin.inp('infra_monthly'), 0) * 12 / 365.0
             + case when extract(day from d.day) = 1 and d.day >= fin.inp_date('bank_paid_from')
                    then coalesce(fin.inp('bank_fee_monthly'), 0) else 0 end
             + case when d.day in (select m_start from months) then coalesce(fin.contractor_monthly(), 0) else 0 end
             + coalesce(t.amount, 0) as fixed_out,
           coalesce(p.amount, 0) as plans_out
    from days d cross join h cross join left_by l cross join capital c
    left join fin.v_yk_transit_daily y on y.day = d.day
    left join taxes t on t.day = d.day
    left join plans p on p.day = d.day
)
select f.day, f.capital_in, f.payouts_in, f.budget_out, f.ads_out, f.fixed_out,
       s.balance + sum(f.capital_in + f.payouts_in - f.budget_out - f.ads_out - f.fixed_out - f.plans_out)
           over (order by f.day) as closing,
       fin.inp('min_cash_reserve') as reserve,
       -- новые столбцы (010)
       s.balance + sum(f.payouts_in - f.budget_out - f.ads_out - f.fixed_out - f.plans_out) over (order by f.day)
           as closing_no_capital,
       -- новый столбец (013)
       f.plans_out
from flows f cross join s;



create or replace view fin.v_weekly_forecast as
with w as (
    select ((day - current_date - 1) / 7) + 1 as week_no, min(day) as week_start,
           sum(capital_in + payouts_in) as inflow, sum(budget_out + ads_out + fixed_out + plans_out) as outflow,
           (array_agg(closing order by day desc))[1] as closing, min(closing) as lowest, max(reserve) as reserve
    from fin.v_runway_daily group by 1
)
select week_no, week_start, closing - inflow + outflow as opening, inflow, outflow, closing, lowest,
       lowest < reserve as below_reserve
from w where week_no <= 8 order by week_no;

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
       -- новые столбцы (010)
       case when b.balance < fin.inp('min_cash_reserve') then b.day else r.runout_no_capital end as runout_no_capital,
       (select remaining from fin.v_capital_status) as capital_remaining,
       -- 013: и открытые плановые платежи до старта — «осталось» в бюджете их уже не содержит.
       (select coalesce(sum(greatest(coalesce(left_amount, 0), 0)), 0) from fin.v_pilot_budget_status
         where kind in ('разовая', 'реклама'))
       + (select coalesce(sum(amount), 0) from fin.v_plans
           where not closed and due_day < fin.inp_date('pilot_start_date')) as left_before_start,
       -- новый столбец (013): «Денег хватит до» словами — дата или «на весь прогноз».
       case when b.balance < fin.inp('min_cash_reserve') then 'уже ниже резерва'
            when r.runout is not null then to_char(r.runout, 'DD.MM.YYYY')
            else 'на весь прогноз (до ' || to_char(hz.horizon, 'DD.MM.YYYY') || ')' end as runway_text
from b cross join r cross join hz;



create or replace view fin.v_attention as
with ps as (select * from fin.v_pilot_status),
money as (select * from fin.v_money_now),
checks as (
    select 1 as sort,
           case when m.free_cash + m.capital_remaining < m.left_before_start then 'Срочно' else 'Важно' end as level,
           'Денег не хватает на траты до старта: осталось потратить ' || fin.fmt_rub(m.left_before_start)
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
)
select sort, level, text, place from checks
union all
select 0, 'Всё в порядке', 'Проверки ничего не нашли', null where not exists (select 1 from checks)
order by 1;


-- --- FCF ---------------------------------------------------------------------------------------
-- Денежный поток по периодам (grain: 'month' или 'week'):
--   операционный — выписка (операционные статьи) + покупки «мимо счёта» по их статьям;
--   вложения — оборудование: со счёта и «мимо счёта»;
--   FCF = операционный + вложения;
--   финансирование — вклады, выплаты партнёрам, переводы между своими счетами; в FCF не входит.
-- Перевод себе «на покупки» — не расход сам по себе: он разносится по тому, что на него
-- купили «мимо счёта» за тот же период. Неразнесённый остаток — операционный расход
-- (unallocated; строка «Не разнесены покупки с карты» в «Требует внимания»). Покупок больше,
-- чем переведено, — разницу партнёр оплатил из своих: это финансирование.
-- Так остаток счёта сходится: его изменение = операционный + вложения + финансирование.
create or replace function fin.cash_flows(grain text default 'month')
returns table (period date, operating numeric, investing numeric, fcf numeric, financing numeric,
               unallocated numeric, contributions numeric, cum_fcf numeric, cum_contributions numeric,
               paid_orders bigint, operating_per_order numeric)
language sql stable as $$
with g as (select case when grain in ('week', 'неделя', 'неделям') then 'week' else 'month' end as g),
bank as (
    select date_trunc(g.g, b.day)::date as period,
           coalesce(sum(b.amount_signed) filter (where b.cash_class = 'Операционные'), 0) as op,
           coalesce(sum(b.amount_signed) filter (where b.cash_class = 'Вложения'), 0) as inv,
           coalesce(sum(b.amount_signed) filter (where b.cash_class = 'Финансирование'
               or (b.cash_class = 'Переводы между своими счетами' and b.transfer_kind is distinct from 'на покупки')), 0) as fin,
           coalesce(sum(b.amount) filter (where b.transfer_kind = 'на покупки'), 0) as to_card,
           coalesce(sum(b.amount) filter (where b.article_id = 'cff_owner_contribution' and b.direction = 'in'), 0) as contrib
    from fin.v_bank_ops b cross join g group by 1
),
card as (
    select date_trunc(g.g, x.date)::date as period,
           coalesce(sum(x.amount) filter (where e.cash_class <> 'Вложения'), 0) as op,
           coalesce(sum(x.amount) filter (where e.cash_class = 'Вложения'), 0) as inv
    from fin.expenses x join fin.v_expense_items e on e.id = x.id cross join g
    where not x.is_plan and x.paid_from = 'мимо счёта' group by 1
),
orders as (
    select date_trunc(g.g, o.paid_date_msk)::date as period, count(*) as n
    from analytics.v_orders o cross join g where o.is_paid group by 1
),
f as (
    select p.period,
           coalesce(b.to_card, 0) - coalesce(c.op, 0) - coalesce(c.inv, 0) as left_on_card,
           coalesce(b.op, 0) - coalesce(c.op, 0) as op, coalesce(b.inv, 0) - coalesce(c.inv, 0) as inv,
           coalesce(b.fin, 0) as fin, coalesce(b.contrib, 0) as contrib, coalesce(o.n, 0) as n
    from (select period from bank union select period from card) p
    left join bank b using (period) left join card c using (period) left join orders o using (period)
)
select f.period,
       f.op - greatest(f.left_on_card, 0) as operating,
       f.inv as investing,
       f.op - greatest(f.left_on_card, 0) + f.inv as fcf,
       f.fin + greatest(-f.left_on_card, 0) as financing,
       greatest(f.left_on_card, 0) as unallocated,
       f.contrib as contributions,
       sum(f.op - greatest(f.left_on_card, 0) + f.inv) over (order by f.period) as cum_fcf,
       sum(f.contrib) over (order by f.period) as cum_contributions,
       f.n as paid_orders,
       -- После старта: операционный поток на один оплаченный заказ.
       case when f.n > 0 and f.period >= date_trunc((select g from g), fin.inp_date('pilot_start_date'))::date
            then (f.op - greatest(f.left_on_card, 0)) / f.n end as operating_per_order
from f order by f.period
$$;
comment on function fin.cash_flows(text) is
    'Денежный поток по месяцам или неделям: операционный, вложения, FCF, финансирование, неразнесённое с карты, накопленные FCF и вклады.';

create or replace view fin.v_fcf_monthly as select * from fin.cash_flows('month');
create or replace view fin.v_fcf_weekly as select * from fin.cash_flows('week');
comment on view fin.v_fcf_monthly is 'FCF по месяцам: fin.cash_flows(''month'').';
comment on view fin.v_fcf_weekly is 'FCF по неделям: fin.cash_flows(''week'').';

-- Платежи впереди: плановые расходы и налоги из календаря на 8 недель.
create or replace view fin.v_payments_ahead as
select p.due_day as day, p.plan_date, p.what, p.amount, p.budget_item, 'план' as source, p.closed, p.overdue
from fin.v_plans p where not p.closed and p.due_day <= current_date + 56
union all
select p.closed_on, p.plan_date, p.what, p.amount, p.budget_item, 'план', true, false
from fin.v_plans p where p.closed and p.plan_date >= current_date - 7
union all
select greatest(t.due_date, current_date + 1), t.due_date, t.event, t.amount_hint, null, 'налоги', false, t.due_date < current_date
from fin.tax_calendar t
where t.status = 'ожидает' and t.due_date <= current_date + 56;
comment on view fin.v_payments_ahead is
    'Платежи на 8 недель: открытые планы (просроченный — на завтра), недавно закрытые и налоги из календаря.';

insert into fin.schema_migrations (version) values ('013_plans_fcf') on conflict do nothing;
