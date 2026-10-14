-- 002: сырые данные из банка. Пишет только finance_sync.

\set ON_ERROR_STOP on

-- Операции ровно как их отдал банк. Источник правды, ничего не удаляем.
CREATE TABLE IF NOT EXISTS finance.raw_operations (
    operation_id        text PRIMARY KEY,
    account_number      text        NOT NULL,
    operation_date      date        NOT NULL,
    operation_ts        timestamptz,
    direction           text        NOT NULL CHECK (direction IN ('in', 'out')),
    amount              numeric(18, 2) NOT NULL CHECK (amount >= 0),
    -- Поступление со знаком «+», списание со знаком «−»: сумма по периоду
    -- сразу даёт изменение остатка.
    amount_signed       numeric(18, 2) GENERATED ALWAYS AS
                            (CASE WHEN direction = 'in' THEN amount ELSE -amount END) STORED,
    counterparty_name   text,
    counterparty_inn    text,
    counterparty_account text,
    purpose             text,
    tbank_category      text,
    operation_status    text,
    raw_json            jsonb       NOT NULL,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS raw_operations_date_idx
    ON finance.raw_operations (operation_date);
CREATE INDEX IF NOT EXISTS raw_operations_account_date_idx
    ON finance.raw_operations (account_number, operation_date);
CREATE INDEX IF NOT EXISTS raw_operations_inn_idx
    ON finance.raw_operations (counterparty_inn);

COMMENT ON TABLE finance.raw_operations IS
    'Операции из GET /api/v1/statement. Только operationStatus=Transaction.';

-- Остатки, которые банк вернул вместе с выпиской (withBalances=true).
-- Нужны как якорь для дневных остатков и для сверки check_diff.
CREATE TABLE IF NOT EXISTS finance.balances (
    account_number  text        NOT NULL,
    as_of_date      date        NOT NULL,
    balance_kind    text        NOT NULL CHECK (balance_kind IN ('opening', 'closing')),
    amount          numeric(18, 2) NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    raw_json        jsonb,
    PRIMARY KEY (account_number, as_of_date, balance_kind)
);

COMMENT ON TABLE finance.balances IS
    'Остатки от банка. closing на дату D — остаток на конец дня D.';

-- Докуда синхронизирован каждый счёт.
CREATE TABLE IF NOT EXISTS finance.sync_state (
    account_number    text PRIMARY KEY,
    synced_through    date,
    last_run_at       timestamptz,
    last_success_at   timestamptz,
    last_status       text,
    last_error        text,
    operations_seen   bigint NOT NULL DEFAULT 0
);

-- Параметры расчётов: пороги, ставки. Правятся вручную или через import-rules.
CREATE TABLE IF NOT EXISTS finance.settings (
    key         text PRIMARY KEY,
    value       text NOT NULL,
    comment     text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO finance.settings (key, value, comment) VALUES
    ('capex_threshold', '10000',
     'Порог CAPEX в рублях: покупки дороже и со сроком пользы > 12 мес. считаются инвестициями'),
    ('vat_threshold_usn', '60000000',
     'Порог дохода на УСН, после которого возникает НДС (проверять актуальность на год)'),
    ('contrib_1pct_threshold', '300000',
     'Порог дохода, свыше которого платится 1% дополнительных взносов'),
    ('runway_months', '3',
     'За сколько месяцев считать средний операционный отток для runway')
ON CONFLICT (key) DO NOTHING;

INSERT INTO finance.schema_migrations (version)
VALUES ('002_raw_tables')
ON CONFLICT (version) DO NOTHING;
