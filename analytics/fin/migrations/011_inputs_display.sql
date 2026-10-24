-- 011: «Служебное → Вводные» — короткие названия тем (не обрезаются в колонке) и
-- «Предположение» — это допущение, а не факт. Только представление, данные не меняются.

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
                'delivery_acquiring_markup') then '2. Комиссии'
           when key like 'pouch%' or key like '%label%' or key in ('code_price', 'packing_loss_rate', 'packaging_limit',
                'codes_per_set4x35_packs', 'kin_codes_per_box', 'other_variable_per_order') or key like 'packaging%'
                then '4. Упаковка'
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
       case when status ~* 'допущ|предлож|предполож|сценар|рабоч|нет данных|оценк|диапазон|временн|пример|проект|уточн|сверить'
                 and status !~* 'решени' then 'допущение'
            when status ~* 'решени|план|согласован|изменено формой|выбран|бюджет' then 'решение'
            else 'факт' end as kind,
       comment_fixed as comment,
       key,
       note like 'Не используется%' as unused
from c;
insert into fin.schema_migrations (version) values ('011_inputs_display') on conflict do nothing;
