-- 004: «Последние записи» — суммы в рублях без копеек, как на остальных карточках.

create or replace view fin.v_recent_entries as
select * from (
    select 'Вводная' as form, created_at, entered_by, key || ' = ' || coalesce(value::text, value_text, '—') as what, note
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

insert into fin.schema_migrations (version) values ('004_recent_entries_rub') on conflict do nothing;
