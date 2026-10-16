-- Роли Metabase на ВМ Timeweb. Заменяет datalens_role.sql: DataLens больше нет.
--
-- Запускается от владельца базы, пароли — из переменных окружения:
--
--   docker exec -i -e MB_RO_PW="$(cat /etc/teashop/metabase_db_password)" \
--     -e MB_APP_PW="$(cat /etc/teashop/metabase_app_password)" \
--     teashop-postgres psql -U teashop -d teashop < analytics/metabase_roles.sql
--
-- metabase_ro — чтение схемы analytics и пяти витрин finance; к рабочим таблицам
-- бота (переписка, pii_vault, заказы) и сырым операциям выписки доступа нет.
-- metabase_app — владелец базы metabase, где Metabase хранит свои настройки.

\set ON_ERROR_STOP on
\getenv ro_pw MB_RO_PW
\getenv app_pw MB_APP_PW

select format('create role metabase_ro login password %L', :'ro_pw')
 where not exists (select from pg_roles where rolname = 'metabase_ro') \gexec
select format('alter role metabase_ro login password %L', :'ro_pw') \gexec
alter role metabase_ro set default_transaction_read_only = on;
alter role metabase_ro set statement_timeout = '60s';
-- Дашборд шлёт десяток запросов разом; Metabase держит не больше 8 подключений
-- (MB_JDBC_DATA_WAREHOUSE_MAX_CONNECTION_POOL_SIZE), с лимитом 5 карточки падали.
alter role metabase_ro connection limit 10;
revoke create on schema public from metabase_ro;
grant usage on schema analytics to metabase_ro;
grant select on all tables in schema analytics to metabase_ro;
alter default privileges for role teashop in schema analytics grant select on tables to metabase_ro;

-- Витрины finance создаёт cashflow (ветка claude/great-darwin-dfl2tn); до неё блок пропускается.
select 'grant usage on schema finance to metabase_ro'
 where exists (select from pg_namespace where nspname = 'finance') \gexec
-- v_daily_balance_total — остаток по дням для кривой на «Сводке» (15.10.2026): только день и сумма.
select 'grant select on finance.v_cash_summary, finance.v_metrics, finance.v_ddc, finance.v_unclassified, finance.v_channel_mix, finance.v_daily_balance_total to metabase_ro'
 where exists (select from pg_namespace where nspname = 'finance') \gexec
-- Холды (16.10.2026, cashflow/migrations/009): физлица в витринах замаскированы, как в v_unclassified.
select 'grant select on finance.v_pending, finance.v_pending_summary to metabase_ro'
 where exists (select from pg_views where schemaname = 'finance' and viewname = 'v_pending') \gexec

select format('create role metabase_app login password %L', :'app_pw')
 where not exists (select from pg_roles where rolname = 'metabase_app') \gexec
select format('alter role metabase_app login password %L', :'app_pw') \gexec
alter role metabase_app connection limit 20;
select 'create database metabase owner metabase_app'
 where not exists (select from pg_database where datname = 'metabase') \gexec
revoke connect on database metabase from public;
