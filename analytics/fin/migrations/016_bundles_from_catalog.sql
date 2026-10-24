-- 016: состав сетов и наборов — один, из каталога бота (shop.bundle_items).
--
-- До 016 состав жил дважды: в каталоге (его правят в Metabase формой
-- «Состав сета/набора», по нему бот резервирует сырьё и собирает набор) и в
-- fin.bundle_items, откуда считалась себестоимость. Правка в Metabase до
-- финмодели не доходила: в каталоге «Следующий шаг» и «Утро и вечер» уже с
-- другой Габой, а маржа считалась по старой.
--
-- Теперь fin.bundles и fin.bundle_items — представления над каталогом:
--   набор      — артикул вида «сет»/«набор», название — из каталога;
--   вложение   — его сырьё (shop.catalog.raw_material) и граммы (фасовка × количество);
--   пакеты     — по пачке на каждое вложение-пачку (блину пакет не нужен),
--                цена пакета — по фасовке вложения (до 50 г — пакет 35 г);
--   коды и этикетки — по одному на вложение и один на коробку, как в v1.5
--                (4 пакетика — 5 кодов).
-- Сырьё вложения, которого нет в справочнике fin.sku, не выпадает молча из
-- себестоимости (раньше inner join его просто терял): у такого набора
-- себестоимости нет вовсе, а «Требует внимания» называет сырьё и артикулы.

do $$
begin
    if to_regclass('shop.bundle_items') is null then
        raise exception 'Нет shop.bundle_items — сначала деплой бота с каталогом в базе';
    end if;
end $$;

-- Что расходится между старым составом и каталогом — в лог применения.
do $$
declare
    r record;
begin
    for r in
        with old as (
            select b.sku_id, i.sku_name as raw, i.grams from fin.bundle_items i join fin.bundles b using (bundle)
            where b.active
        ),
        new as (
            select b.bundle_sku as sku_id, c.raw_material as raw, sum(c.net_weight_g * b.quantity) as grams
            from shop.bundle_items b join shop.catalog c on c.sku_id = b.item_sku
            where b.active group by b.bundle_sku, c.raw_material
        )
        select coalesce(o.sku_id, n.sku_id) as sku_id, o.raw as old_raw, o.grams as old_g, n.raw as new_raw, n.grams as new_g
        from old o full join new n on n.sku_id = o.sku_id and n.raw = o.raw
        where o.grams is distinct from n.grams
        order by 1
    loop
        raise notice 'Состав %: было «%» % г → в каталоге «%» % г', r.sku_id, r.old_raw, r.old_g, r.new_raw, r.new_g;
    end loop;
end $$;

-- На v_catalog_link держатся другие представления (склад): пока таблицы
-- состава заменяются представлениями, она живёт без наборов.
create or replace view fin.v_catalog_link as
select s.sku_name as product, c.sku_id, c.name, c.price, c.gtin, c.on_sale
from fin.sku s
left join lateral (
    select c.* from shop.catalog c
    where c.raw_material = s.sku_name and c.net_weight_g = s.sale_pack_g
      and c.kind in ('пачка', 'блин') and c.active
    order by c.on_sale desc, c.sku_id
    limit 1
) c on true;

drop table fin.bundle_items;
drop table fin.bundles;

create view fin.bundle_items as
select s.name as bundle, c.raw_material as sku_name, sum(c.net_weight_g * b.quantity) as grams,
       b.bundle_sku, b.item_sku, c.kind as item_kind, c.net_weight_g as item_g, sum(b.quantity)::int as quantity
from shop.bundle_items b
join shop.catalog s on s.sku_id = b.bundle_sku
join shop.catalog c on c.sku_id = b.item_sku
where b.active and s.kind in ('сет', 'набор')
group by s.name, c.raw_material, b.bundle_sku, b.item_sku, c.kind, c.net_weight_g;
comment on view fin.bundle_items is
    'Состав сетов и наборов из каталога бота (shop.bundle_items): сырьё и граммы вложения. Правится в Metabase, «Каталог» → «Состав сета/набора».';

create view fin.bundles as
select s.name as bundle, s.kind, s.sku_id,
       coalesce(sum(i.quantity) filter (where i.item_kind = 'пачка'), 0)::int as pouches,
       case when bool_and(i.item_g <= 50) then '35g' else '100g' end as pouch_type,
       coalesce(sum(i.quantity), 0)::int + 1 as codes_total,
       coalesce(sum(i.quantity), 0)::int + 1 as labels_total,
       sum(i.grams) as grams,
       s.active and count(i.item_sku) > 0 as active
from shop.catalog s
left join fin.bundle_items i on i.bundle_sku = s.sku_id
where s.kind in ('сет', 'набор')
group by s.sku_id, s.name, s.kind, s.active;
comment on view fin.bundles is
    'Сеты и наборы каталога бота с составом для себестоимости: пакеты, коды и этикетки (по одному на вложение и коробку). active = в архиве нет и состав есть.';

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
join shop.catalog c on c.sku_id = b.sku_id and c.active;
comment on view fin.v_catalog_link is
    'Товар финмодели → артикул каталога бота: цена, GTIN и название берутся оттуда. sku_id пуст — артикула в каталоге нет.';

-- Артикулы, чьё сырьё (своё или вложений) не найдено в справочнике сырья:
-- себестоимости у них нет.
create view fin.v_catalog_no_cost as
select c.sku_id, c.name, c.raw_material
from shop.catalog c
where c.active and c.kind in ('пачка', 'блин')
  and not exists (select 1 from fin.sku s where s.sku_name = c.raw_material)
  and (c.on_sale or exists (select 1 from shop.bundle_items b join shop.catalog x on x.sku_id = b.bundle_sku
                            where b.item_sku = c.sku_id and b.active and x.active))
union
select x.sku_id, x.name, c.raw_material
from shop.bundle_items b
join shop.catalog x on x.sku_id = b.bundle_sku and x.active
join shop.catalog c on c.sku_id = b.item_sku
where b.active and not exists (select 1 from fin.sku s where s.sku_name = c.raw_material);
comment on view fin.v_catalog_no_cost is
    'Артикулы без себестоимости: сырьё пачки или вложения набора не найдено в fin.sku. Пачка — если в продаже или во вложениях.';

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
    -- L — Claude API, W — комиссия с доставки. Состав — из каталога (016): пакет
    -- по фасовке вложения, у блина ни пакета, ни потерь фасовки. Хоть одно
    -- сырьё не найдено в справочнике — у набора себестоимости нет (having).
    select b.bundle as product, b.kind,
           sum(i.grams) as grams, l.price::numeric as retail_price,
           sum(i.grams * t.tea_rub_per_g / case when i.item_kind = 'блин' then 1 else 1 - r.loss end) as tea_cost,
           sum(i.grams * t.inbound_rub_per_g / case when i.item_kind = 'блин' then 1 else 1 - r.loss end) as inbound_cost,
           b.pouches,
           coalesce(sum(i.quantity * case when i.item_kind = 'блин' then 0
                                          when i.item_g <= 50 then r.pouch_35 else r.pouch_100 end), 0) as pouch_cost,
           b.codes_total as codes, b.codes_total * r.code_price as codes_cost,
           b.labels_total as labels, b.labels_total * r.label_unit as labels_cost,
           r.other_var as box_cost,
           r.api + r.subsidy + r.delivery_fees as order_extra_cost,
           min(t.cost_source) as cost_source, l.name as bot_name, l.gtin
    from fin.bundles b
    left join fin.v_catalog_link l on l.product = b.bundle
    join fin.bundle_items i on i.bundle_sku = b.sku_id
    left join fin.v_tea_cost t on t.sku_name = i.sku_name
    cross join r
    where b.active
    group by b.bundle, b.kind, l.price, b.pouches, b.codes_total, b.labels_total, l.name, l.gtin,
             r.code_price, r.label_unit, r.other_var, r.api, r.subsidy, r.delivery_fees
    having bool_and(t.sku_name is not null)
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
comment on function fin.unit_costs(date) is 'Себестоимость и маржа единицы на дату. Листы 2, 3, 4. Цена, GTIN, название и состав наборов — из каталога бота (fin.v_catalog_link, fin.bundle_items).';

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
    union all
    -- 016: себестоимость считается по составу из каталога — сырьё должно быть в справочнике.
    select 23, 'Важно', 'Нет себестоимости: сырьё ' || string_agg(distinct '«' || x.raw_material || '»', ', ')
               || ' не найдено в справочнике сырья финмодели — артикулы ' || string_agg(distinct x.sku_id, ', ')
               || '. Исправьте «Сырьё» в каталоге', 'catalog'
    from fin.v_catalog_no_cost x having count(*) > 0
)
select sort, level, text, place from checks
union all
select 0, 'Всё в порядке', 'Проверки ничего не нашли', null where not exists (select 1 from checks)
order by 1;

insert into fin.schema_migrations (version) values ('016_bundles_from_catalog') on conflict do nothing;
