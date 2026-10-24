-- 014: партии сырья и склад — из бота: shop.lots, shop.reservations, shop.stock_moves.
--
-- Фасуем под каждый заказ: готовых пачек на складе нет, есть сырьё партиями.
-- Партию вносят один раз — формой «Партия закупки» в shop.lots; fin.purchase_lots
-- становится представлением поверх неё (те же столбцы, себестоимость по партиям
-- считается как раньше). Остаток, резерв и списания ведёт бот: резерв при оплате,
-- списание при сборке (app/modules/stock). Форма «Фасовка» больше не нужна;
-- fin.packing_runs остаётся таблицей до блока кодов (на неё ссылается fin.code_batches).
--
-- Порядок на бою: деплой бота (создаёт shop.lots и остальное) → эта миграция.

do $$
begin
    if to_regclass('shop.lots') is null then
        raise exception 'Нет shop.lots — сначала деплой бота со складом по партиям';
    end if;
end $$;

-- Партии, внесённые в fin.purchase_lots до этой миграции, — в shop.lots.
insert into shop.lots (lot_code, raw_material, unit, qty, arrival_date, supplier, price_per_unit, discount,
                       delivery_cost, note, entered_by, created_at)
select 'L-' || to_char(p.date, 'YYMMDD') || '-F' || p.id, p.sku_name,
       case when s.supplier_unit ilike 'пресс%' then 'шт' else 'г' end,
       case when s.supplier_unit ilike 'пресс%' then p.qty_units else p.qty_units * s.unit_weight_kg * 1000 end,
       p.date, p.supplier, p.price_per_unit, p.discount, p.delivery_cost,
       concat_ws(' ', p.note, '(перенесено из fin.purchase_lots)'), p.entered_by, p.created_at
from fin.purchase_lots p join fin.sku s using (sku_name)
where not exists (select 1 from shop.lots l where l.lot_code = 'L-' || to_char(p.date, 'YYMMDD') || '-F' || p.id);

drop view if exists fin.v_attention, fin.v_recent_entries, fin.v_stock, fin.v_tea_cost;
drop table fin.purchase_lots;

create view fin.purchase_lots as
select l.id, l.arrival_date as date, l.raw_material as sku_name, l.supplier,
       (case when l.unit = 'г' then l.qty / 1000 / coalesce(s.unit_weight_kg, 1) else l.qty end)::numeric(12,3) as qty_units,
       l.price_per_unit, l.discount, l.delivery_cost, l.note, l.entered_by, l.created_at,
       -- новые столбцы (014)
       l.lot_code, l.unit, l.best_before
from shop.lots l left join fin.sku s on s.sku_name = l.raw_material
where l.active;
comment on view fin.purchase_lots is
    'Партии закупки — представление над shop.lots (форма «Партия закупки»). qty_units — в закупочных единицах: кг рассыпного, штуки блинов.';

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


-- Сырьё в работе: остатки партий, резерв, расход за 14 дней. Единица — как у партии
-- (граммы рассыпного, штуки блинов).
create or replace view fin.v_raw_stock as
with b as (
    select raw_material, unit, sum(qty_in) as bought, sum(on_hand) as on_hand, sum(reserved) as reserved,
           sum(free) as free, count(*) filter (where on_hand > 0) as lots
    from shop.v_lot_balance where active group by raw_material, unit
),
used as (
    select r.raw_material, sum(r.qty) filter (where r.packed_at >= now() - interval '14 days') / 14.0 as daily_14d,
           count(*) filter (where r.status = 'списан') as packed, count(*) filter (where r.status = 'резерв') as in_work
    from shop.reservations r group by r.raw_material
),
-- До старта продаж нет: расход — по плану. Заказов в день — на безубыточность М1 из
-- вводных, на заказ — одна пачка основной фасовки, поровну между сортами в продаже.
plan as (
    select (select orders_needed from fin.v_breakeven_plan where month_no = 1) / 30.0
           / nullif((select count(distinct raw_material) from shop.catalog where on_sale and active
                     and kind in ('пачка', 'блин')), 0) as orders_per_raw
),
main as (
    select distinct on (raw_material) raw_material, net_weight_g, kind
    from shop.catalog where active and kind in ('пачка', 'блин') and price is not null
    order by raw_material, on_sale desc, net_weight_g desc
)
select s.sku_name as raw_material, b.unit, coalesce(b.bought, 0) as bought, coalesce(b.on_hand, 0) as on_hand,
       coalesce(b.reserved, 0) as reserved, coalesce(b.free, 0) as free, coalesce(b.lots, 0) as lots,
       coalesce(u.packed, 0) as packed, coalesce(u.in_work, 0) as in_work,
       m.net_weight_g as main_pack,
       case when coalesce(u.daily_14d, 0) > 0 then u.daily_14d
            when not (select started from fin.v_pilot_status)
                 then p.orders_per_raw * case when m.kind = 'блин' then 1 else m.net_weight_g end end as daily_use,
       coalesce(u.daily_14d, 0) > 0 as daily_from_sales
from fin.sku s
left join b on b.raw_material = s.sku_name
left join used u on u.raw_material = s.sku_name
left join main m on m.raw_material = s.sku_name
cross join plan p
where s.active;
comment on view fin.v_raw_stock is
    'Сырьё: куплено, на складе, в резерве, свободно (в единицах партии), расход в день — за 14 дней или по плану до старта.';

-- Точка заказа по сырью: срок поставки + фасовка + запас, умноженные на расход в день.
create or replace view fin.v_reorder_raw as
with x as (
    select r.*, s.supplier_unit, s.unit_weight_kg,
           coalesce(
               (select t.lead_days from fin.supply_lead_times t where t.scope = 'sku' and t.name = r.raw_material
                  and t.valid_from <= current_date order by t.valid_from desc, t.created_at desc limit 1),
               (select t.lead_days from fin.supply_lead_times t
                 where t.scope = 'поставщик' and t.valid_from <= current_date
                   and t.name = (select supplier from shop.lots l where l.raw_material = r.raw_material
                                  and supplier is not null order by arrival_date desc limit 1)
                 order by t.valid_from desc, t.created_at desc limit 1),
               fin.inp('supply_lead_days')::int) as lead_days
    from fin.v_raw_stock r join fin.sku s on s.sku_name = r.raw_material
),
y as (
    -- Нет срока поставки — точки заказа нет («нет данных»), а не «не пора».
    select x.*, (x.lead_days + coalesce(fin.inp('packing_marking_days'), 0) + coalesce(fin.inp('safety_stock_days'), 0))
                * x.daily_use as need
    from x where x.daily_use > 0
)
select raw_material, unit, free, daily_use, free / nullif(daily_use, 0) as days_left, lead_days, need,
       free < need as order_now,
       -- Заказать до кратности поставщика: мешки по 1 кг или штуки блинов.
       ceil((need - free) / case when unit = 'шт' then 1 else unit_weight_kg * 1000 end) as order_units,
       case when unit = 'шт' then 'шт' else 'мешк. по ' || trim_scale(unit_weight_kg)::text || ' кг' end as order_unit,
       trim_scale(round(free))::text || ' ' || unit as free_shown,
       ceil((need - free) / case when unit = 'шт' then 1 else unit_weight_kg * 1000 end)::text || ' '
           || case when unit = 'шт' then 'шт' else 'мешк. по ' || trim_scale(unit_weight_kg)::text || ' кг' end as order_shown
from y;
comment on view fin.v_reorder_raw is
    'Точка заказа сырья: order_now — свободного меньше, чем уйдёт за (срок поставки + фасовка + запас) дней; пусто — нет срока поставки. Сколько заказать — до кратности поставщика.';

create or replace view fin.v_lots_expiring as
select b.lot_id, b.lot_code, b.raw_material, b.unit, b.on_hand, b.best_before, b.best_before - current_date as days_left
from shop.v_lot_balance b
where b.active and b.on_hand > 0 and b.best_before is not null and b.best_before < current_date + 90;
comment on view fin.v_lots_expiring is 'Партии, у которых до «годен до» меньше 90 дней, и сколько в них осталось.';

-- Расхождения: инвентаризация и потери фасовки по партиям.
create or replace view fin.v_stock_discrepancies as
select m.created_at, l.lot_code, l.raw_material, l.unit,
       case m.kind when 'корректировка' then 'инвентаризация' else 'потери фасовки' end as kind,
       case when m.kind = 'корректировка' then m.qty else -m.qty end as diff,
       case when m.kind = 'корректировка' then m.qty else -m.qty end / nullif(l.qty, 0) as diff_share,
       m.note, m.created_by
from shop.stock_moves m join shop.lots l on l.id = m.lot_id
where m.kind in ('корректировка', 'потери');
comment on view fin.v_stock_discrepancies is 'Инвентаризации (разница со знаком) и потери фасовки по партиям, доля — от прихода партии.';

-- Владелец включил продажу, а сырья не хватит ни на одну единицу: бот такой артикул не
-- продаёт (то же правило, что в app/modules/stock/service.py: срок пачки ≥ MIN_SHELF_LIFE_DAYS=90).
create or replace view fin.v_on_sale_no_stock as
with free as (
    select raw_material, unit, sum(free) as free from shop.v_lot_balance
    where active and free > 0 and (best_before is null or best_before >= current_date + 90)
    group by raw_material, unit
),
need as (
    select c.sku_id, c.raw_material, case when c.kind = 'блин' then 'шт' else 'г' end as unit,
           case when c.kind = 'блин' then 1 else c.net_weight_g end as qty
    from shop.catalog c where c.on_sale and c.active and c.kind in ('пачка', 'блин')
    union all
    select b.bundle_sku, i.raw_material, case when i.kind = 'блин' then 'шт' else 'г' end,
           sum(b.quantity * case when i.kind = 'блин' then 1 else i.net_weight_g end)
    from shop.bundle_items b join shop.catalog s on s.sku_id = b.bundle_sku and s.on_sale and s.active
    join shop.catalog i on i.sku_id = b.item_sku
    where b.active group by b.bundle_sku, i.raw_material, i.kind
)
select c.sku_id, c.name, c.kind
from shop.catalog c
where c.on_sale and c.active
  and (exists (select 1 from need n left join free f on f.raw_material = n.raw_material and f.unit = n.unit
               where n.sku_id = c.sku_id and coalesce(f.free, 0) < n.qty)
       or (c.kind in ('сет', 'набор') and not exists (select 1 from need n where n.sku_id = c.sku_id)));
comment on view fin.v_on_sale_no_stock is 'Артикулы в продаже, на которые не хватает свободного сырья: бот их не продаёт.';

-- Склад для «Отчётов» и точки заказа — по сырью (готовых пачек на складе больше нет:
-- «на руках» — пачки в работе, то есть резерв под оплаченные и не собранные заказы).
create or replace view fin.v_stock as
with sold as (
    select fin_product as product,
           sum(quantity) filter (where not is_refunded) as sold,
           sum(quantity) filter (where is_refunded) as refunded
    from fin.v_order_lines where has_cost group by fin_product
)
select r.raw_material as product,
       case when s.supplier_unit ilike 'пресс%' then 'блин' else 'пачка' end as kind,
       r.bought as grams_bought, r.bought - r.on_hand as grams_used, r.on_hand as grams_left,
       r.packed, so.sold, so.refunded, r.in_work as on_hand,
       r.daily_use as avg_daily_sales_14d,
       r.free / nullif(r.daily_use, 0) as days_left,
       coalesce(r.free / nullif(r.daily_use, 0) < 14, false) as reorder,
       o.lead_days,
       fin.inp('packing_marking_days')::int as packing_days,
       fin.inp('safety_stock_days')::int as safety_days,
       o.need as reorder_point,
       o.order_now,
       not exists (select 1 from fin.v_catalog_link l where l.product = r.raw_material and l.on_sale) as not_in_bot
from fin.v_raw_stock r
join fin.sku s on s.sku_name = r.raw_material
left join sold so on so.product = r.raw_material
left join fin.v_reorder_raw o on o.raw_material = r.raw_material;
comment on view fin.v_stock is
    'Склад по сырью (миграция 014): единицы партии — граммы рассыпного, штуки блинов; on_hand — пачки в работе (резерв); точка заказа — fin.v_reorder_raw.';

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
)
select sort, level, text, place from checks
union all
select 0, 'Всё в порядке', 'Проверки ничего не нашли', null where not exists (select 1 from checks)
order by 1;



insert into fin.schema_migrations (version) values ('014_stock_lots') on conflict do nothing;
