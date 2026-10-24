-- 015: коды «Честного знака» по статусам — из журнала бота (shop.code_events).
--
-- При фасовке под заказ код вводится в оборот после сборки: наклеен → выгрузка
-- «Производство РФ» → «Введено» → отгрузка и закрывающий чек. Журнал, выгрузки и
-- текущий статус ведёт бот (app/modules/marking/schema.py); финмодель их только
-- читает. Прежние партии кодов (fin.code_batches, fin.code_batch_items — ввод
-- заранее, до сборки), списания (fin.code_writeoffs) и фасовка (fin.packing_runs)
-- больше не нужны: списания перенесены в журнал «испорчен», формы убраны.
--
-- Порядок на бою: деплой бота (создаёт shop.code_events и остальное) → эта миграция.

do $$
begin
    if to_regclass('shop.code_events') is null then
        raise exception 'Нет shop.code_events — сначала деплой бота с журналом кодов';
    end if;
end $$;

-- Наша декларация о соответствии (ИП — изготовитель, ТР ТС 021, схема 3д): номер и
-- дата идут в выгрузку на каждый код. Настоящей декларации ещё нет (24.10.2026) —
-- тестовая, чтобы опробовать выгрузку. Номер с «ТЕСТ»: пока он такой, /codes и
-- «Требует внимания» предупреждают, что в ЛК ЧЗ такую выгрузку подавать нельзя.
-- Настоящую — формой «Изменить вводную».
insert into fin.inputs (key, label, value_text, status, note, valid_from, entered_by)
select v.key, v.label, v.value, 'Допущение', v.note, '2026-10-24', 'миграция'
from (values
    ('chz_declaration_number', 'Декларация о соответствии: номер', 'ТЕСТ ЕАЭС N RU Д-CN.ТЕСТ.В.00000/26',
     'Тестовая. Заменить номером настоящей декларации до первой подачи в ЛК ЧЗ'),
    ('chz_declaration_date', 'Декларация о соответствии: дата', '24.10.2026',
     'Тестовая. Заменить датой настоящей декларации до первой подачи в ЛК ЧЗ')
) as v (key, label, value, note)
where not exists (select 1 from fin.inputs i where i.key = v.key);

-- Списания кодов из прежней формы — в журнал «испорчен».
insert into shop.code_events (gtin, serial, status, at, by, note)
select w.gtin, w.serial, 'spoiled', w.created_at, w.entered_by, w.reason || coalesce(' · ' || w.note, '')
from fin.code_writeoffs w
where not exists (select 1 from shop.code_events e where e.gtin = w.gtin and e.serial = w.serial and e.status = 'spoiled');

drop view if exists fin.v_codes, fin.v_code_batches, fin.v_recent_entries, fin.v_attention;
drop table if exists fin.code_batch_items, fin.code_writeoffs, fin.code_batches, fin.packing_runs;

-- Коды по GTIN — прежние столбцы для «Отчётов».
create or replace view fin.v_codes as
select s.gtin,
       count(*) filter (where s.pool_code is not null) as imported,
       count(*) filter (where s.order_id is not null and s.status not in ('spoiled', 'withdrawn')) as used_in_orders,
       count(*) filter (where s.status in ('received', 'printed') and s.order_id is null) as available,
       count(*) filter (where s.status = 'returned') as returned,
       count(*) filter (where s.status = 'printed') as printed,
       count(*) filter (where exists (select 1 from shop.code_batch_items i join shop.code_batches b on b.id = i.batch_id
                                      where i.gtin = s.gtin and i.serial = s.serial and b.status = 'выгружена')) as submitted,
       count(*) filter (where s.status = 'introduced') as in_circulation,
       count(*) filter (where s.status in ('spoiled', 'withdrawn')) as written_off,
       count(*) filter (where s.status = 'sold' and not exists (
           select 1 from shop.code_events e where e.gtin = s.gtin and e.serial = s.serial and e.status = 'introduced'))
           as sold_not_in_circulation
from shop.v_code_status s group by s.gtin;
comment on view fin.v_codes is 'Коды ЧЗ по GTIN из журнала бота: получено из СУЗ, в заказах, свободно, в выгрузке, введено, списано.';

create or replace view fin.v_code_batches as
select b.id, b.created_at, b.status, count(i.serial) as codes_count, b.introduced_at, b.introduced_by, b.document
from shop.code_batches b left join shop.code_batch_items i on i.batch_id = b.id
group by b.id order by b.id desc;
comment on view fin.v_code_batches is 'Выгрузки для ввода в оборот («Производство РФ»): статус и сколько кодов.';

-- Артикул по GTIN — для подписей.
create or replace view fin.v_code_articles as
select distinct on (lpad(c.gtin, 14, '0')) lpad(c.gtin, 14, '0') as gtin, c.sku_id, c.name
from shop.catalog c where c.gtin is not null order by lpad(c.gtin, 14, '0'), c.active desc, c.sku_id;

-- Артикул × статус.
create or replace view fin.v_codes_by_status as
select s.gtin, a.sku_id, a.name, s.status,
       case s.status when 'ordered' then 'заказан' when 'received' then 'получен' when 'printed' then 'напечатан'
            when 'applied' then 'наклеен' when 'introduced' then 'введён в оборот' when 'sold' then 'продан'
            when 'returned' then 'возвращён' when 'spoiled' then 'испорчен' when 'withdrawn' then 'выведен из оборота' end
           as status_name,
       count(*) as codes
from shop.v_code_status s left join fin.v_code_articles a on a.gtin = s.gtin
group by s.gtin, a.sku_id, a.name, s.status;
comment on view fin.v_codes_by_status is 'Сколько кодов в каждом статусе по артикулу (GTIN).';

-- Деньги за коды: куплено (получено из СУЗ) и испорчено, по цене кода из вводных.
create or replace view fin.v_codes_money as
select count(distinct (gtin, serial)) filter (where status = 'received') as bought,
       count(distinct (gtin, serial)) filter (where status = 'received') * fin.inp('code_price') as bought_rub,
       count(distinct (gtin, serial)) filter (where status = 'spoiled') as spoiled,
       count(distinct (gtin, serial)) filter (where status = 'spoiled') * fin.inp('code_price') as spoiled_rub,
       fin.inp('code_price') as code_price
from shop.code_events;
comment on view fin.v_codes_money is 'Куплено кодов (получены из СУЗ) и испорчено — штуки и рубли по вводной code_price.';

-- Нельзя продавать: наклеен, а в оборот не введён дольше рабочего дня.
create or replace view fin.v_codes_unsellable as
select s.gtin, s.serial, s.order_id, s.status_at as applied_at, a.name
from shop.v_code_status s left join fin.v_code_articles a on a.gtin = s.gtin
where s.status = 'applied'
  and fin.next_business_day((s.status_at at time zone 'Europe/Moscow')::date) < current_date;
comment on view fin.v_codes_unsellable is 'Коды наклеены и не введены в оборот дольше рабочего дня: эти пачки нельзя продавать.';

-- Застряли: введён в оборот, не продан дольше 3 дней — при фасовке под заказ такого быть не должно.
create or replace view fin.v_codes_stuck as
select s.gtin, s.serial, s.order_id, s.status_at as introduced_at, a.name
from shop.v_code_status s left join fin.v_code_articles a on a.gtin = s.gtin
where s.status = 'introduced' and s.status_at < now() - interval '3 days';
comment on view fin.v_codes_stuck is 'Коды введены в оборот и не проданы дольше 3 дней: потерянная или не отправленная пачка.';

-- Хватит ли кодов: свободные в пуле против наклеенных за 14 дней.
create or replace view fin.v_codes_runway as
with free as (
    select gtin, count(*) as free from shop.v_code_status
    where status in ('received', 'printed') and order_id is null group by gtin
),
used as (
    select gtin, count(*) / 14.0 as per_day from shop.code_events
    where status = 'applied' and at >= now() - interval '14 days' group by gtin
)
select coalesce(f.gtin, u.gtin) as gtin, a.sku_id, a.name, coalesce(f.free, 0) as free, u.per_day,
       coalesce(f.free, 0) / nullif(u.per_day, 0) as days_left
from free f full join used u using (gtin)
left join fin.v_code_articles a on a.gtin = coalesce(f.gtin, u.gtin);
comment on view fin.v_codes_runway is 'Свободные коды в пуле против продаж (наклеено) за 14 дней по GTIN; days_left < 14 — пора заказывать в СУЗ.';

-- По партии сырья: что стало с пачками, в которые она ушла.
create or replace view fin.v_codes_by_lot as
select l.lot_code, l.raw_material,
       count(distinct (s.gtin, s.serial)) filter (where s.status not in ('received', 'printed', 'ordered')) as packed,
       count(distinct (s.gtin, s.serial)) filter (where exists (select 1 from shop.code_events e where e.gtin = s.gtin
                                                  and e.serial = s.serial and e.status = 'introduced')) as introduced,
       count(distinct (s.gtin, s.serial)) filter (where s.status in ('sold', 'returned')) as sold,
       count(distinct (s.gtin, s.serial)) filter (where s.status = 'returned') as returned,
       count(distinct (s.gtin, s.serial)) filter (where s.status = 'sold') as at_customers
from shop.v_code_lots l join shop.v_code_status s on s.gtin = l.gtin and s.serial = l.serial
group by l.lot_code, l.raw_material;
comment on view fin.v_codes_by_lot is 'По партии сырья: пачек собрано, введено в оборот, продано, возвращено, у покупателей.';

-- Коды партии — для выгрузки при отзыве партии или выводе из оборота.
create or replace view fin.v_lot_codes as
select distinct l.lot_code, '01' || s.gtin || '21' || s.serial as ki, s.gtin, s.serial, s.status, s.order_id
from shop.v_code_lots l join shop.v_code_status s on s.gtin = l.gtin and s.serial = l.serial;
comment on view fin.v_lot_codes is 'Все коды пачек из партии сырья (через резерв пачки) — для отзыва партии.';

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
    -- 015: выгрузки кодов для ввода в оборот («Фасовки» больше нет — фасуем под заказ).
    union all select case b.status when 'введена' then 'Коды введены в оборот' when 'отменена' then 'Выгрузка отменена'
                             else 'Выгрузка кодов' end,
                     coalesce(b.introduced_at, b.created_at), coalesce(b.introduced_by, b.created_by),
                     'выгрузка №' || b.id || ': ' || (select count(*) from shop.code_batch_items i where i.batch_id = b.id)
                         || ' кодов', b.document
              from shop.code_batches b
    -- 014: инвентаризация, порча и возврат на складе — тоже формами.
    union all select case m.kind when 'корректировка' then 'Инвентаризация' when 'порча' then 'Списать сырьё'
                             else 'Возврат на склад' end,
                     m.created_at, m.created_by, l.lot_code || ': ' || trim_scale(m.qty)::text || ' ' || l.unit, m.note
              from shop.stock_moves m join shop.lots l on l.id = m.lot_id
              where m.kind in ('корректировка', 'порча', 'возврат') and m.created_by <> 'бот'
    union all select 'Реклама за день', created_at, entered_by,
                     to_char(date, 'DD.MM.YYYY') || ' ' || channel || ': ' || fin.fmt_rub(spend_own) || ' своих', note from fin.ad_spend
    union all select 'Акт перевозчика', created_at, entered_by, carrier || ' ' || track || ': ' || fin.fmt_rub(amount), kind
              from fin.carrier_acts
    union all select 'Срок поставки', created_at, entered_by, scope || ' ' || name || ': ' || lead_days || ' дн.', note
              from fin.supply_lead_times
) e order by created_at desc limit 20;

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
    from fin.inputs_current where key = 'chz_declaration_number' and value_text ilike '%тест%'
    union all
    select 21, case when r.days_left < 7 then 'Срочно' else 'Важно' end,
           'Кодов ЧЗ хватит на ' || round(r.days_left) || ' дн.: GTIN ' || r.gtin || coalesce(' «' || r.name || '»', '')
               || ' — свободно ' || r.free || ', закажите в СУЗ', 'codes'
    from fin.v_codes_runway r where r.days_left < 14
)
select sort, level, text, place from checks
union all
select 0, 'Всё в порядке', 'Проверки ничего не нашли', null where not exists (select 1 from checks)
order by 1;

insert into fin.schema_migrations (version) values ('015_code_statuses') on conflict do nothing;
