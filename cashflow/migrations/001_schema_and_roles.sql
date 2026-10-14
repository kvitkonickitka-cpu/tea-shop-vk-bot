-- 001: схема finance и роли с минимальными правами.
--
-- Эту миграцию применяет СУПЕРПОЛЬЗОВАТЕЛЬ (postgres) один раз, вручную:
--   psql -U postgres -d <база> -v sync_password=ПАРОЛЬ1 -v ro_password=ПАРОЛЬ2 \
--        -f migrations/001_schema_and_roles.sql
--
-- Ничего в существующих схемах не меняется: создаётся новая схема finance
-- и две новые роли. Роли приложения (бота) к finance доступа не получают.

\set ON_ERROR_STOP on

-- Роль, от которой работает скрипт синхронизации. Владелец схемы finance.
SELECT format('CREATE ROLE finance_sync LOGIN PASSWORD %L', :'sync_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finance_sync')
\gexec

-- Роль для DataLens. Только SELECT и только на витрины (гранты в 006).
SELECT format('CREATE ROLE datalens_ro LOGIN PASSWORD %L', :'ro_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'datalens_ro')
\gexec

CREATE SCHEMA IF NOT EXISTS finance AUTHORIZATION finance_sync;

-- Ни одна из новых ролей не должна видеть чужие схемы и создавать объекты в public.
REVOKE ALL ON SCHEMA public FROM finance_sync, datalens_ro;
REVOKE ALL ON SCHEMA finance FROM PUBLIC;

GRANT USAGE ON SCHEMA finance TO datalens_ro;

-- datalens_ro не должна иметь возможности создавать что-либо в finance.
REVOKE CREATE ON SCHEMA finance FROM datalens_ro;

-- Подключаться к базе нужно обеим ролям, но не более того.
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO finance_sync, datalens_ro',
                   current_database());
END
$$;

-- Таблица учёта применённых миграций. Дальнейшие миграции применяет finance_sync.
CREATE TABLE IF NOT EXISTS finance.schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE finance.schema_migrations OWNER TO finance_sync;

INSERT INTO finance.schema_migrations (version)
VALUES ('001_schema_and_roles')
ON CONFLICT (version) DO NOTHING;
