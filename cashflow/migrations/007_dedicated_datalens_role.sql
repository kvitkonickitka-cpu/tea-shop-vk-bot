-- 007: отдельная роль для DataLens под финансовые витрины.
--
-- Роль datalens_ro уже существовала в базе до cashflow и используется другими
-- дашбордами (переписка с клиентами, заказы, агент) — у неё есть SELECT на
-- таблицы схемы public через ALTER DEFAULT PRIVILEGES, настроенный владельцем
-- базы отдельно от cashflow. Подключать финансовые витрины к той же роли нельзя:
-- по правилам проекта доступ к таблицам магазина выдаётся только по отдельному
-- явному согласию, а не заодно с DataLens для финансов.
--
-- Эта миграция заводит отдельную роль finance_datalens_ro, видит только пять
-- финансовых витрин и ничего больше, и снимает с datalens_ro гранты на витрины
-- finance, которые были по ошибке выданы ей в 006 (её собственные дашборды
-- эти гранты не используют).
--
-- Применяет СУПЕРПОЛЬЗОВАТЕЛЬ (или POSTGRES_USER контейнера) один раз, вручную:
--   docker exec -i КОНТЕЙНЕР psql -U АДМИН -d ИМЯ_БАЗЫ \
--        -v ro_password=ПАРОЛЬ < migrations/007_dedicated_datalens_role.sql

\set ON_ERROR_STOP on

SELECT format('CREATE ROLE finance_datalens_ro LOGIN PASSWORD %L', :'ro_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finance_datalens_ro')
\gexec

REVOKE ALL ON SCHEMA public FROM finance_datalens_ro;
GRANT USAGE ON SCHEMA finance TO finance_datalens_ro;
REVOKE CREATE ON SCHEMA finance FROM finance_datalens_ro;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO finance_datalens_ro',
                   current_database());
END
$$;

GRANT SELECT ON
    finance.v_cash_summary,
    finance.v_metrics,
    finance.v_ddc,
    finance.v_unclassified,
    finance.v_channel_mix
TO finance_datalens_ro;

ALTER DEFAULT PRIVILEGES FOR ROLE finance_sync IN SCHEMA finance
    REVOKE ALL ON TABLES FROM finance_datalens_ro;

-- Убираем у старой роли то, что ей выдали в 006: её дашборды это не используют,
-- а мы не хотим, чтобы одна роль видела и переписку с клиентами, и финансы.
REVOKE ALL ON
    finance.v_cash_summary,
    finance.v_metrics,
    finance.v_ddc,
    finance.v_unclassified,
    finance.v_channel_mix
FROM datalens_ro;

INSERT INTO finance.schema_migrations (version)
VALUES ('007_dedicated_datalens_role')
ON CONFLICT (version) DO NOTHING;
