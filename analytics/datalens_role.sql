-- Роль для Yandex DataLens: только чтение схемы analytics.
--
-- Запускается один раз на виртуалке с базой, от владельца базы (teashop):
--
--   read -rs DATALENS_DB_PASSWORD && export DATALENS_DB_PASSWORD
--   docker exec -i -e DATALENS_DB_PASSWORD teashop-postgres \
--     psql -U teashop -d teashop < datalens_role.sql
--
-- Пароль берётся из переменной окружения и в командную строку не попадает:
-- `-e DATALENS_DB_PASSWORD` без значения передаёт переменную из окружения.
-- Повторный запуск безопасен: роль не пересоздаётся, пароль обновляется.
--
-- Представления принадлежат владельцу базы и читают рабочие таблицы с его
-- правами, поэтому роли достаточно SELECT на сами представления: к
-- таблицам с персональными данными (orders.details, история переписки)
-- у неё доступа нет.

\set ON_ERROR_STOP on
\getenv datalens_password DATALENS_DB_PASSWORD
\if :{?datalens_password}
\else
  \echo 'Нет переменной DATALENS_DB_PASSWORD — роль не создана.'
  \quit
\endif

select format('create role datalens_reader login password %L', :'datalens_password')
where not exists (select from pg_roles where rolname = 'datalens_reader') \gexec
select format('alter role datalens_reader login password %L', :'datalens_password') \gexec

-- Только чтение, без долгих запросов и без лишних подключений.
alter role datalens_reader set default_transaction_read_only = on;
alter role datalens_reader set statement_timeout = '60s';
alter role datalens_reader connection limit 5;

-- Рабочие таблицы в public роли не видны: прав на них не выдаём.
revoke create on schema public from datalens_reader;
create schema if not exists analytics;
grant usage on schema analytics to datalens_reader;
grant select on all tables in schema analytics to datalens_reader;
-- Новые представления схемы — тоже сразу на чтение.
alter default privileges in schema analytics grant select on tables to datalens_reader;

\echo 'Роль datalens_reader готова: только SELECT на схему analytics.'
