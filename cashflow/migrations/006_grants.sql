-- 006: права. datalens_ro видит только витрины и ничего больше.
--
-- Представления в PostgreSQL исполняются с правами их владельца (finance_sync),
-- поэтому DataLens читает агрегаты, не имея доступа к сырым операциям.

\set ON_ERROR_STOP on

-- На всякий случай снимаем всё, что могло быть выдано раньше.
REVOKE ALL ON ALL TABLES IN SCHEMA finance FROM datalens_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA finance FROM datalens_ro;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA finance FROM datalens_ro;

GRANT SELECT ON
    finance.v_cash_summary,
    finance.v_metrics,
    finance.v_ddc,
    finance.v_unclassified,
    finance.v_channel_mix
TO datalens_ro;

-- Новые таблицы, созданные finance_sync, не должны автоматически становиться
-- доступными для чтения из DataLens.
ALTER DEFAULT PRIVILEGES FOR ROLE finance_sync IN SCHEMA finance
    REVOKE ALL ON TABLES FROM datalens_ro;

INSERT INTO finance.schema_migrations (version)
VALUES ('006_grants')
ON CONFLICT (version) DO NOTHING;
