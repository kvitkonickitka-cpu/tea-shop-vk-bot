-- 004: результат классификации. Пересобирается командой `classify`, сырьё не трогает.

\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS finance.fact_operations (
    operation_id      text NOT NULL REFERENCES finance.raw_operations (operation_id)
                          ON DELETE CASCADE,
    -- Платёж по кредиту делится на две строки: проценты и тело. Остальные — одна строка.
    split_no          smallint NOT NULL DEFAULT 1,
    account_number    text NOT NULL,
    operation_date    date NOT NULL,
    direction         text NOT NULL CHECK (direction IN ('in', 'out')),
    amount            numeric(18, 2) NOT NULL CHECK (amount >= 0),
    amount_signed     numeric(18, 2) GENERATED ALWAYS AS
                          (CASE WHEN direction = 'in' THEN amount ELSE -amount END) STORED,
    article_id        text NOT NULL REFERENCES finance.articles (article_id),
    -- Откуда взялась статья: ручная разметка, правило, разбивка кредита или «не разобрано».
    classified_by     text NOT NULL CHECK (classified_by IN
                          ('manual', 'rule', 'loan_split', 'unclassified')),
    rule_id           bigint,
    counterparty_name text,
    counterparty_inn  text,
    -- org — юрлицо (ИНН 10 знаков), person — физлицо или ИП (12), unknown — ИНН нет.
    -- В витринах всё, кроме org, маскируется.
    counterparty_kind text NOT NULL DEFAULT 'unknown'
                          CHECK (counterparty_kind IN ('org', 'person', 'unknown')),
    purpose           text,
    flags             text[] NOT NULL DEFAULT '{}',
    classified_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, split_no)
);

CREATE INDEX IF NOT EXISTS fact_operations_date_idx
    ON finance.fact_operations (operation_date);
CREATE INDEX IF NOT EXISTS fact_operations_article_idx
    ON finance.fact_operations (article_id, operation_date);

COMMENT ON COLUMN finance.fact_operations.flags IS
    'Метки для разбора: loan_amount_mismatch, loan_schedule_missing и т.п.';

INSERT INTO finance.schema_migrations (version)
VALUES ('004_fact_operations')
ON CONFLICT (version) DO NOTHING;
