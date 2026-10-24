-- 021: Габа снова «Вулканическая» (IT-162), «Габа Эко» — в архив. Решение владельца 24.10.2026:
-- I-TEA отгружает IT-162 от 0,5 кг, поставка 7–10 дней. Цена 3 750 ₽/кг, план первой
-- закупки 1,5 кг. В каталоге бота пачки P-GABA-100 и P-GABA-35 переведены на это сырьё
-- отдельно (правка каталога с записью в журнал), без них у наборов с Габой не было бы
-- себестоимости.

update fin.sku set active = true, price_per_unit_base = 3750, qty_units_first_purchase = 1.5
 where sku_name = 'Габа Вулканическая (IT-162)';
update fin.sku set active = false where sku_name = 'Габа Эко';

insert into fin.schema_migrations (version) values ('021_gaba_it162_back') on conflict do nothing;
