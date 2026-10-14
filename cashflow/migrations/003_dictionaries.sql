-- 003: справочники. Наполняются командой `import-rules` из CSV, руками ничего писать не нужно.

\set ON_ERROR_STOP on

-- Статьи ДДС.
CREATE TABLE IF NOT EXISTS finance.articles (
    article_id  text PRIMARY KEY,
    article     text NOT NULL,
    section     text NOT NULL CHECK (section IN ('CFO', 'CFI', 'CFF', 'TECH')),
    direction   text NOT NULL CHECK (direction IN ('in', 'out', 'any')),
    grp         text,
    is_capex    boolean NOT NULL DEFAULT false,
    comment     text
);

COMMENT ON COLUMN finance.articles.direction IS
    'Для какого направления денег применима статья: in — приход, out — расход, any — оба';
COMMENT ON COLUMN finance.articles.grp IS
    'Группа для витрин: «Товар», «Маркетинг», «Сервисы», «Налоги» и т.п.';

-- Правила классификации. Порядок — по priority, срабатывает первое совпадение.
CREATE TABLE IF NOT EXISTS finance.rules (
    rule_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    priority    integer NOT NULL,
    field       text NOT NULL CHECK (field IN (
                    'counterparty_inn', 'counterparty_name', 'purpose',
                    'tbank_category', 'direction', 'account')),
    match_type  text NOT NULL CHECK (match_type IN ('equals', 'contains', 'regex')),
    value       text NOT NULL,
    direction   text CHECK (direction IN ('in', 'out')),
    amount_min  numeric(18, 2),
    amount_max  numeric(18, 2),
    article_id  text NOT NULL REFERENCES finance.articles (article_id),
    active      boolean NOT NULL DEFAULT true,
    comment     text,
    UNIQUE (priority, field, match_type, value, article_id)
);

CREATE INDEX IF NOT EXISTS rules_priority_idx ON finance.rules (priority) WHERE active;

-- Ручная разметка конкретных операций. Приоритет выше любого правила.
CREATE TABLE IF NOT EXISTS finance.manual_overrides (
    operation_id text PRIMARY KEY,
    article_id   text NOT NULL REFERENCES finance.articles (article_id),
    comment      text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- График платежей по кредиту: платёж приходит одной суммой, делим на проценты и тело.
CREATE TABLE IF NOT EXISTS finance.loan_schedule (
    loan_id       text    NOT NULL,
    period_no     integer NOT NULL,
    due_date      date    NOT NULL,
    payment_total numeric(18, 2) NOT NULL,
    interest      numeric(18, 2) NOT NULL,
    principal     numeric(18, 2) NOT NULL,
    balance_after numeric(18, 2),
    comment       text,
    PRIMARY KEY (loan_id, period_no),
    CONSTRAINT loan_schedule_parts_match
        CHECK (abs(payment_total - interest - principal) <= 0.01)
);

CREATE INDEX IF NOT EXISTS loan_schedule_due_date_idx ON finance.loan_schedule (due_date);

COMMENT ON TABLE finance.loan_schedule IS
    'Аннуитет: проценты → CFO, тело → CFF. Несовпадение суммы платежа попадает в v_unclassified.';

INSERT INTO finance.schema_migrations (version)
VALUES ('003_dictionaries')
ON CONFLICT (version) DO NOTHING;
