-- 005: «Последние записи» — вводная по русскому названию с единицей, а не ключом (usd_rub = 87).

create or replace view fin.v_recent_entries as
select * from (
    -- Вводная — по названию из модели и с единицей: «Курс доллара: 87 ₽/$»;
    -- доли — процентами («3.4 %»), даты и текст — как вписаны.
    select 'Вводная' as form, created_at, entered_by,
           coalesce(label, key) || ': ' ||
           case when value is null then coalesce(value_text, 'нет данных')
                when unit = '%' then fin.fmt_pct(value)
                else trim_scale(value)::text || coalesce(' ' || nullif(unit, ''), '') end as what, note
    from fin.inputs where entered_by <> 'импорт v1.5'
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
) e order by created_at desc limit 20;

insert into fin.schema_migrations (version) values ('005_recent_entries_labels') on conflict do nothing;
