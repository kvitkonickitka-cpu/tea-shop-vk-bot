-- 017: Габа в ассортименте — «Габа Эко» (IT-26), а не «Габа Вулканическая» (IT-162).
--
-- В каталоге бота (Metabase) у пачек Габы и у наборов «Следующий шаг» и «Утро и
-- вечер» сырьё «Габа Эко»; в справочнике сырья его не было, и с миграции 016 у
-- них нет себестоимости («Требует внимания», строка 23). Решение владельца
-- 24.10.2026: оставляем Габу Эко. Закупочная цена — 3400 ₽ за кг без скидок
-- (Никита); план первой закупки — тот же, что был у IT-162 (1,5 кг). IT-162 в
-- каталоге нет — в архив, из бюджета первой закупки она уходит.

insert into fin.sku (sku_name, category, supplier_unit, unit_weight_kg, price_per_unit_base,
                     qty_units_first_purchase, sale_pack_g, role, entered_by)
select 'Габа Эко', category, supplier_unit, unit_weight_kg, 3400, qty_units_first_purchase, sale_pack_g, role, 'Никита'
from fin.sku where sku_name = 'Габа Вулканическая (IT-162)'
on conflict (sku_name) do nothing;

update fin.sku set active = false where sku_name = 'Габа Вулканическая (IT-162)';

insert into fin.product_names (product, display_name, supplier_code)
values ('Габа Эко', 'Габа Эко', 'IT-26')
on conflict (product) do nothing;

insert into fin.schema_migrations (version) values ('017_gaba_eco') on conflict do nothing;
