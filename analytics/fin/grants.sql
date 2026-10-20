-- Права на схему fin. Выполняется после создания ролей (roles.sql) и после
-- каждой миграции с новыми таблицами (apply.sh делает это сам).
--
-- metabase_fin — подключение Metabase «Финмодель (ввод)», через него
-- работают формы (Actions). Пишет только в таблицы ввода fin.*, вводные —
-- только добавляет (история не перезаписывается). К схемам public,
-- analytics и finance доступа нет вовсе: представления fin читают их
-- правами владельца, а функции fin трогают только таблицы fin.
--
-- metabase_ro — прежнее подключение дашбордов: читает fin как analytics.
--
-- Проверка: tests/test_fin_model.py::test_metabase_fin_writes_only_fin.

grant usage on schema fin to metabase_fin, metabase_ro;
revoke create on schema fin from public;

-- Чтение: представления и таблицы fin (в них нет данных клиентов). Таблицы
-- нужны и для чтения: функции fin выполняются с правами того, кто спрашивает.
grant select on all tables in schema fin to metabase_fin, metabase_ro;
alter default privileges in schema fin grant select on tables to metabase_fin, metabase_ro;

-- Запись — только формы.
grant insert on fin.inputs to metabase_fin;
grant insert, update, delete on fin.expenses, fin.capital, fin.cash_snapshots, fin.purchase_lots,
      fin.packing_runs, fin.ad_spend, fin.plan to metabase_fin;
grant usage on all sequences in schema fin to metabase_fin;

-- 006: акт перевозчика и сроки поставки — формы; налоговый календарь — отметка формой.
grant insert, update, delete on fin.carrier_acts, fin.supply_lead_times to metabase_fin;
grant update (status, paid_amount, done_on, done_by, note) on fin.tax_calendar to metabase_fin;
