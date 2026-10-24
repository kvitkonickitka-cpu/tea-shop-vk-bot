-- 010: доработки «Пульса» и «Служебного» по ревью 21.10.2026.
--
-- - вклад по выписке привязывается к партнёру разметкой операции (fin.bank_marks.partner):
--   второй записи о поступлении нет, 80 000 ₽ не считаются дважды;
-- - ответы владельца 21.10: взнос 80 000 ₽ (сентябрь) — Никиты; перевод себе 21.09 на
--   1 000 ₽ — вывод владельцем, а не покупка;
-- - разметка операции по полям: последняя непустая строка по каждому полю (партнёр не
--   стирает статью бюджета, поставленную раньше);
-- - «Требует внимания»: уровни словами, любой непокрытый перевод на покупки старше N
--   дней, переводы себе без разметки, «Не сопоставлено», вклад без партнёра; Claude —
--   только по полным дням с загруженным счётом и не меньше трёх таких дней;
-- - прогноз: закупка чая и пополнение ВК — на свои даты (вводные), выплаты ЮKassa в пути
--   без возвращённых платежей (тестовый платёж 26.09 на 217 ₽ с возвратом давал −8,25 ₽
--   в «Придёт»), второй прогноз — без ожидаемых вкладов;
-- - бюджет пилота: потрачено до старта и в пилоте отдельно;
-- - вводные для дашборда: факт / решение / допущение, дата из источника, опечатки импорта;
--   взносы 2026 — «решение», а не «подтверждено бухгалтером».
--
-- Существующие представления — CREATE OR REPLACE с теми же столбцами (новые — в конце),
-- записи людей в fin не меняются: исправления — новыми строками.

-- --- разметка операции: чей вклад ------------------------------------------------------
alter table fin.bank_marks add column if not exists partner text check (partner in ('Никита', 'Илья'));
alter table fin.bank_marks drop constraint if exists bank_marks_something;
alter table fin.bank_marks drop constraint if exists bank_marks_something_v2;
alter table fin.bank_marks add constraint bank_marks_something_v2
    check (transfer_kind is not null or budget_item is not null or partner is not null);
comment on column fin.bank_marks.partner is 'Чей вклад: «Взнос собственника» по выписке без второй записи о поступлении.';

-- Ответы владельца 21.10.2026. Операции есть только на бою; в другой базе строки ни с чем не совпадут.
insert into fin.bank_marks (operation_id, partner, note, entered_by)
select '595740b2-3ba2-0007-97f7-fc2765ef5454', 'Никита', 'Ответ владельца 21.10.2026: взнос 80 000 ₽ — вклад Никиты', 'миграция'
where not exists (select 1 from fin.bank_marks where operation_id = '595740b2-3ba2-0007-97f7-fc2765ef5454' and partner is not null);
insert into fin.bank_marks (operation_id, transfer_kind, note, entered_by)
select '5d5f3cfc-5a3e-0089-ab34-e47fadb46f08', 'вывод владельцем', 'Ответ владельца 21.10.2026: вывод денег, не покупка', 'миграция'
where not exists (select 1 from fin.bank_marks where operation_id = '5d5f3cfc-5a3e-0089-ab34-e47fadb46f08' and transfer_kind is not null);

-- --- вводные --------------------------------------------------------------------------
insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select * from (values
    ('tea_purchase_date', 'Дата закупки чая', null::numeric,
     to_char(coalesce(fin.inp_date('pilot_start_date'), date '2026-11-15') - 14, 'YYYY-MM-DD'), 'дата', 'Допущение',
     'За 14 дней до старта; закупка и доставка партии в прогнозе денег — этим днём', '2026-10-21'::date, 'миграция'),
    ('vk_topup_date', 'Дата пополнения кабинета ВК', null,
     to_char(coalesce(fin.inp_date('pilot_start_date'), date '2026-11-15') - 7, 'YYYY-MM-DD'), 'дата', 'Допущение',
     'За 7 дней до старта; пополнение ВК в прогнозе денег — этим днём', '2026-10-21', 'миграция')
) as v (key, label, value, value_text, unit, status, note, valid_from, entered_by)
where not exists (select 1 from fin.inputs i where i.key = v.key);

-- Взносы 2026: расчёт сделан не бухгалтером — новая строка поверх «Подтверждено бухгалтером».
insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select 'ip_fixed_contrib_2026', 'Фиксированные взносы ИП за 2026', 57390, null, '₽/год', 'Решение',
       'Расчёт 21.10: за неполный 2026 — 18 014 ₽, срок 28.12.2026; сверить с бухгалтером', '2026-10-21', 'миграция'
where not exists (select 1 from fin.inputs where key = 'ip_fixed_contrib_2026' and status = 'Решение');
update fin.tax_calendar
   set note = 'ИП с 08.09.2026 — пропорционально. Расчёт 21.10: 18 014 ₽; сверить с бухгалтером'
 where due_date = '2026-12-28' and kind = 'взносы' and note like '%подтверждён бухгалтером%';

-- Две вводные на одну ставку карт: модель (fin.payment_rate) читает acquiring_rate, а
-- acquiring_rate_bank_card лишь повторяла её. Без значения способ «карта» берёт
-- acquiring_rate (fin.payment_rate_for) — ставка та же, 3,4 %.
insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)
select 'acquiring_rate_bank_card', 'Эквайринг: банковская карта', null, null, '%', 'Нет данных',
       'Не используется: для карт действует «Карты: эквайринг без НДС» (acquiring_rate)', '2026-10-21', 'миграция'
where not exists (select 1 from fin.inputs where key = 'acquiring_rate_bank_card' and note like 'Не используется%');

-- --- товары: короткие имена, полное — в каталоге поставщика ----------------------------------
update fin.product_names set display_name = v.short
from (values ('Пуэр Банжань Гушу >3лет, 357гр (IT-75)', 'Пуэр Банжань Гушу'),
             ('Пуэр Танцующая Пара Баньна Фенг Мэнхай, 357гр (IT-101)', 'Пуэр Танцующая Пара'),
             ('Пуэр фермерский Сладкий Шен, горы Мэнхая', 'Пуэр Сладкий Шен'),
             ('Да Хун Пао, сильная прожарка (XFI0005)', 'Да Хун Пао')) as v (product, short)
where fin.product_names.product = v.product;

-- --- цвета статей: различимые, «Не сопоставлено» — серый, вложения — одним оттенком --------
update fin.budget_articles set color = v.color
from (values ('Первая закупка чая', '#4E9A06'), ('Упаковка и расходники (лимит)', '#E5A50A'),
             ('Термопринтер', '#3465A4'), ('Весы', '#3465A4'), ('ККТ, ФН, фискализация', '#3465A4'),
             ('ВК: пополнение кабинета', '#9141AC'), ('Подрядчик за пилот', '#C061CB'),
             ('Доставка основной партии', '#8F5902'), ('УКЭП / ЭДО / маркировка', '#CE5C00'),
             ('GTIN / карточки товаров', '#E66100'), ('Сервисы за пилот', '#2AA1B3'),
             ('Взносы 2026: расчётный резерв', '#C01C28'), ('Обслуживание банка за пилот', '#865E3C'),
             ('API тестов и консультаций до продаж', '#26A269'), ('Тестовые отправки до рекламы', '#F66151'),
             ('Не сопоставлено', '#9A9996')) as v (item, color)
where fin.budget_articles.item = v.item;

-- --- операции выписки -------------------------------------------------------------------
create or replace view fin.v_bank_ops as
with m as (
    -- По каждому полю — последняя непустая разметка.
    select operation_id,
           (array_agg(transfer_kind order by created_at desc) filter (where transfer_kind is not null))[1] as transfer_kind,
           (array_agg(budget_item order by created_at desc) filter (where budget_item is not null))[1] as budget_item,
           (array_agg(partner order by created_at desc) filter (where partner is not null))[1] as partner
    from fin.bank_marks group by operation_id
),
ops as (
    select f.operation_id, f.split_no, f.operation_date as day, f.direction, f.amount,
           case when f.direction = 'in' then f.amount else -f.amount end as amount_signed,
           f.article_id, a.article, a.section, a.is_capex,
           m.transfer_kind as marked_kind, m.budget_item as marked_item, m.partner
    from finance.fact_operations f
    join finance.articles a using (article_id)
    left join m using (operation_id)
)
select o.operation_id, o.split_no, o.day, o.direction, o.amount, o.amount_signed, o.article_id, o.article, o.section,
       case when o.article_id = 'tech_internal_transfer' and o.direction = 'out'
            then coalesce(o.marked_kind, 'на покупки') end as transfer_kind,
       coalesce(o.marked_item, bm.budget_item,
                case when o.direction = 'out' and o.section in ('CFO', 'CFI') or o.article_id = 'tech_unclassified'
                     then 'Не сопоставлено' end) as budget_item,
       case when o.article_id = 'tech_internal_transfer'
                 then case when o.direction = 'out' and coalesce(o.marked_kind, 'на покупки') = 'вывод владельцем'
                           then 'Финансирование' else 'Переводы между своими счетами' end
            when o.section = 'CFF' or o.article_id = 'tech_loan_payment' then 'Финансирование'
            when o.section = 'CFI' or o.is_capex or ba.cash_class = 'Вложения' then 'Вложения'
            else 'Операционные' end as cash_class,
       o.marked_item is not null or o.marked_kind is not null or o.partner is not null as is_marked,
       -- новые столбцы (010)
       o.partner,
       -- Перевод себе, вид которого никто не подтвердил: «на покупки» — только по умолчанию.
       o.article_id = 'tech_internal_transfer' and o.direction = 'out' and o.marked_kind is null as transfer_kind_default
from ops o
left join lateral (
    select x.budget_item from fin.budget_article_map x
    where x.source = 'выписка' and x.match_key = o.article_id and x.match_text is null
    order by x.priority, x.id limit 1
) bm on true
left join fin.budget_articles ba on ba.item = coalesce(o.marked_item, bm.budget_item);

-- Перевод на покупки «просрочен», если он старше N дней и не покрыт покупками
-- «мимо счёта» — любой, а не только последний: переводы по порядку дат, покупки — общей суммой.
create or replace view fin.v_card_reconcile_status as
with t as (
    select day, sum(amount) over (order by day, operation_id) as transferred_upto
    from fin.v_bank_ops where transfer_kind = 'на покупки'
),
r as (select coalesce(sum(amount), 0) as recorded from fin.expenses where not is_plan and paid_from = 'мимо счёта')
select coalesce((select sum(transferred) from fin.v_card_reconcile), 0) as transferred,
       r.recorded,
       coalesce((select sum(transferred) from fin.v_card_reconcile), 0) - r.recorded as unreconciled,
       (select max(day) from t) as last_transfer,
       exists (select 1 from t where t.day <= current_date - coalesce(fin.inp('card_unreconciled_days')::int, 3)
                                 and t.transferred_upto > r.recorded) as overdue
from r;

-- --- вклады партнёров ---------------------------------------------------------------------
-- Чей вклад — разметкой операции выписки (поле «Чей вклад») или формой «Вклад / выплата
-- партнёра»; обе — разметка одних и тех же денег, поэтому внесено всего = больше из
-- «по выписке» и «размечено», а неразмеченный остаток — «Не указано, чей».
create or replace view fin.v_partner_capital as
with bank as (select coalesce(sum(amount), 0) as bank_in from fin.v_bank_ops where article_id = 'cff_owner_contribution'),
marked as (
    select partner, sum(contributed) as contributed, sum(paid_out) as paid_out from (
        select partner, amount as contributed, 0::numeric as paid_out
        from fin.v_bank_ops where article_id = 'cff_owner_contribution' and partner is not null
        union all
        select partner, case when kind = 'вклад' then amount else 0 end, case when kind = 'выплата' then amount else 0 end
        from fin.capital
    ) x group by partner
),
partners (partner, plan_key, sort) as (values ('Никита', 'capital_nikita', 1), ('Илья', 'capital_ilya', 2))
select p.partner, p.sort, fin.inp(p.plan_key) as plan, coalesce(m.contributed, 0) as contributed,
       coalesce(m.paid_out, 0) as paid_out
from partners p left join marked m using (partner)
union all
select 'Не указано, чей', 3, null,
       greatest(0, b.bank_in - (select coalesce(sum(contributed), 0) from marked)), 0
from bank b
where b.bank_in > (select coalesce(sum(contributed), 0) from marked);

-- --- бюджет пилота: до старта и в пилоте ---------------------------------------------------
create or replace view fin.v_pilot_budget_status as
with spent as (
    select budget_item, sum(amount) as spent,
           sum(amount) filter (where day < fin.inp_date('pilot_start_date')) as before_start,
           sum(amount) filter (where day >= fin.inp_date('pilot_start_date')) as in_pilot
    from fin.v_spend
    where day >= fin.inp_date('ip_registration_date')
      and day < coalesce(fin.pilot_month_start(coalesce(fin.inp('pilot_months')::int, 3) + 1), 'infinity'::date)
    group by 1
)
select b.line_no, b.item, ba.kind, b.plan_amount as plan, coalesce(s.spent, 0) as spent,
       case when b.plan_amount is not null then b.plan_amount - coalesce(s.spent, 0) end as left_amount,
       coalesce(s.spent, 0) / nullif(b.plan_amount, 0) as spent_share, ba.sort, ba.color,
       -- новые столбцы (010)
       coalesce(s.before_start, 0) as spent_before_start, coalesce(s.in_pilot, 0) as spent_in_pilot
from fin.v_pilot_budget b
join fin.budget_articles ba on ba.item = b.item
left join spent s on s.budget_item = b.item
union all
select null, 'Не сопоставлено', 'нет', null, s.spent, null, null, 99, '#9A9996',
       coalesce(s.before_start, 0), coalesce(s.in_pilot, 0)
from spent s where s.budget_item = 'Не сопоставлено' and s.spent > 0;

-- --- прогноз денег ---------------------------------------------------------------------------
-- Выплаты ЮKassa в пути: оплаченные и не выплаченные платежи без возврата. Возвращённый
-- до выплаты платёж ничего не принесёт, а его возврат и комиссия уже не «в пути»
-- (тестовый платёж 26.09.2026 на 217 ₽ с возвратом давал −8,25 ₽ в «Придёт»).
create or replace view fin.v_yk_transit_daily as
select greatest(fin.next_business_day((p.captured_at at time zone 'Europe/Moscow')::date), current_date + 1) as day,
       sum(p.income_amount) as amount
from finance.yk_payments p
where p.status = 'succeeded' and p.payout_date is null and p.income_amount is not null
  and not exists (select 1 from finance.yk_refunds r where r.payment_id = p.id and r.status = 'succeeded')
group by 1;

-- Прогноз до конца пилота + 30 дней. Крупные разовые траты — на свои даты: закупка
-- чая и доставка партии — tea_purchase_date, пополнение ВК — vk_topup_date (деньги
-- уходят пополнением кабинета, а не дневным расходом рекламы). Остальные разовые —
-- поровну от прихода вкладов до старта. closing_no_capital — тот же прогноз без
-- ожидаемых вкладов.
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
             + coalesce(t.amount, 0) as fixed_out
    from days d cross join h cross join left_by l cross join capital c
    left join fin.v_yk_transit_daily y on y.day = d.day
    left join taxes t on t.day = d.day
)
select f.day, f.capital_in, f.payouts_in, f.budget_out, f.ads_out, f.fixed_out,
       s.balance + sum(f.capital_in + f.payouts_in - f.budget_out - f.ads_out - f.fixed_out) over (order by f.day) as closing,
       fin.inp('min_cash_reserve') as reserve,
       -- новые столбцы (010)
       s.balance + sum(f.payouts_in - f.budget_out - f.ads_out - f.fixed_out) over (order by f.day) as closing_no_capital
from flows f cross join s;

-- Одна строка на день: факт с первого дня с деньгами, прогноз — с последнего факта.
create or replace view fin.v_balance_chart as
with fact as (
    select day, balance from finance.v_daily_balance_total
    where day >= (select min(day) from finance.v_daily_balance_total where balance <> 0)
),
last_fact as (select day, balance from fact order by day desc limit 1),
rows_ as (
    select day, balance as actual, null::numeric as forecast, null::numeric as forecast_no_capital from fact
    union all
    select day, null, balance, balance from last_fact
    union all
    select day, null, closing, closing_no_capital from fin.v_runway_daily
)
select day, max(actual) as actual, max(forecast) as forecast, fin.inp('min_cash_reserve') as reserve,
       max(forecast_no_capital) as forecast_no_capital
from rows_ group by day;

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
       (select coalesce(sum(greatest(coalesce(left_amount, 0), 0)), 0) from fin.v_pilot_budget_status
         where kind in ('разовая', 'реклама')) as left_before_start
from b cross join r cross join hz;

-- --- Claude: сравнимые дни -----------------------------------------------------------------
-- Сравнивать счёт с учётом бота можно только за полный день учёта (не первый день
-- учёта и не сегодня) и когда счёт за день уже загружен.
create or replace view fin.v_claude_daily as
with bill as (select day, sum(amount_usd) as usd from analytics.v_anthropic_costs group by 1),
bot as (select (at at time zone 'UTC')::date as day, sum(cost_usd) as usd from analytics.v_llm_usage group by 1),
first_bot as (select min(day) as day from bot)
select coalesce(bill.day, bot.day) as day,
       bill.usd * fin.inp('usd_rub') as bill_rub, bot.usd * fin.inp('usd_rub') as bot_rub,
       (coalesce(bill.usd, 0) - coalesce(bot.usd, 0)) * fin.inp('usd_rub') as unaccounted_rub,
       bot.day is not null as bot_counted,
       -- новый столбец (010)
       bot.day is not null and bill.day is not null and coalesce(bill.day, bot.day) > (select day from first_bot)
           and coalesce(bill.day, bot.day) < (now() at time zone 'UTC')::date as comparable
from bill full join bot on bot.day = bill.day;

create or replace view fin.v_claude_status as
select sum(bill_rub) as bill_rub, sum(bot_rub) as bot_rub,
       (sum(bill_rub) - sum(bot_rub)) / nullif(sum(bill_rub), 0) as unaccounted_share,
       count(*) as days
from fin.v_claude_daily
where day >= current_date - 7 and comparable;

-- --- водопад маржи: короткие подписи ---------------------------------------------------------
create or replace view fin.v_unit_waterfall as
with u as (
    select c.*, fin.inp('usn_rate') + fin.payment_rate() as fees, fin.api_per_new_buyer() as api
    from fin.v_unit_cost c
)
select product, step_no, step, amount from u
cross join lateral (values
    (1, 'Цена', u.retail_price),
    (2, 'Налог и комиссия', -u.retail_price * u.fees),
    (3, 'Чай', -(u.tea_cost + u.inbound_cost)),
    (4, 'Упаковка', -(u.pouch_cost + u.codes_cost + u.labels_cost + u.box_cost)),
    (5, 'Доставка', -(u.order_extra_cost - u.api)),
    (6, 'Claude', -u.api)
) as w (step_no, step, amount);

-- --- «Требует внимания» -------------------------------------------------------------------
-- Уровень словами («Срочно», «Важно»), цвет ставит дашборд. Нет проблем — «Всё в порядке».
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
)
select sort, level, text, place from checks
union all
select 0, 'Всё в порядке', 'Проверки ничего не нашли', null where not exists (select 1 from checks)
order by 1;

-- --- вводные для дашборда ------------------------------------------------------------------
-- Как их показывает «Служебное → Вводные»: тема, подпись и комментарий без опечаток
-- импорта («Сохранять30 тыс.»), значение-дата как 01.12.2026, вид — факт / решение /
-- допущение, дата действия из источника (статус вида «Никита 13.10»), иначе — с импорта.
create or replace view fin.v_inputs_display as
with c as (
    select i.*,
           -- Пробел между буквой и цифрой, потерянный при импорте из таблицы.
           regexp_replace(coalesce(i.label, i.key), '([А-Яа-яЁё])([0-9])', '\1 \2', 'g') as label_fixed,
           regexp_replace(concat_ws(' · ', i.status, i.note), '([А-Яа-яЁё])([0-9])', '\1 \2', 'g') as comment_fixed,
           substring(i.status from '([0-9]{1,2}\.[0-9]{2}(\.[0-9]{4})?)') as source_date
    from fin.inputs_current i
)
select case
           when key like 'ip_%' or key like 'usn%' or key like 'kudir%' or key like 'tax%' then '3. Налоги'
           when key like 'capital%' or key in ('min_cash_reserve', 'cash_horizon_months', 'partner_payout_target')
                or key like 'bank%' then '1. Деньги'
           when key like 'acquiring%' or key in ('vat_on_fee', 'fiscal_rate', 'fiscal_units', 'planning_payment_method',
                'delivery_acquiring_markup') then '2. Цены и комиссии'
           when key like 'pouch%' or key like '%label%' or key in ('code_price', 'packing_loss_rate', 'packaging_limit',
                'codes_per_set4x35_packs', 'kin_codes_per_box', 'other_variable_per_order') or key like 'packaging%'
                then '4. Упаковка и коды'
           when key like '%deliver%' or key like 'inbound%' or key like 'supply%' or key like 'carrier%' or key in
                ('test_shipments', 'packing_marking_days', 'safety_stock_days', 'tea_purchase_date') then '5. Логистика'
           when key like 'ad_%' or key like 'cac_%' or key like 'vk_%' or key like 'kpi_%' or key like 'api_%'
                or key in ('first_ad_test_own', 'usd_rub', 'scenario', 'contractor_paid_monthly', 'contractor_months')
                then '6. Реклама'
           else '7. Пилот' end as theme,
       label_fixed as label,
       case when value is null and value_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then to_char(value_text::date, 'DD.MM.YYYY')
            when value is null then coalesce(value_text, 'нет данных')
            when unit = '%' then fin.fmt_pct(value)
            when unit like '₽%' then fin.fmt_rub(value) || coalesce(nullif(substring(unit from 2), ''), '')
            else replace(trim_scale(value)::text, '.', ',') || coalesce(' ' || nullif(unit, ''), '') end as value_shown,
       case when entered_by = 'импорт v1.5' and source_date is not null
                 then source_date || case when source_date ~ '\.[0-9]{4}$' then '' else '.2026' end
            when entered_by = 'импорт v1.5' then 'с импорта v1.5'
            else to_char(valid_from, 'DD.MM.YYYY') end as since,
       case when status ~* 'допущ|предлож|сценар|рабоч|нет данных|оценк|диапазон|временн|пример|проект|уточн|сверить'
                 and status !~* 'решени' then 'допущение'
            when status ~* 'решени|план|согласован|изменено формой|выбран|бюджет' then 'решение'
            else 'факт' end as kind,
       comment_fixed as comment,
       key,
       note like 'Не используется%' as unused
from c;
comment on view fin.v_inputs_display is
    'Вводные для «Служебного»: тема, подпись без опечаток импорта, значение (даты — ДД.ММ.ГГГГ), с какой даты, факт / решение / допущение.';

insert into fin.schema_migrations (version) values ('010_pulse_fixes') on conflict do nothing;
