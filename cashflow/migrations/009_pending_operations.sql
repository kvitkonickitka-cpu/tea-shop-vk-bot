-- 009: операции в холде — отдельно от проведённых.
--
-- Холд (авторизация) — деньги списаны с доступного остатка, но банк ещё не
-- провёл операцию. В ДДС, остатки и check_diff холды не входят: это ещё не
-- движение денег. Но владельцу полезно видеть их вечером того же дня, не
-- дожидаясь проведения, — поэтому отдельная таблица и витрина.
--
-- Таблица — снимок на последнюю выгрузку: sync пишет то, что банк вернул на
-- запрос operationStatus=Authorization, и удаляет остальное. Проведённый
-- холд уходит отсюда и появляется в raw_operations, отменённый — просто уходит.

\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS finance.pending_operations (
    operation_id        text PRIMARY KEY,
    account_number      text        NOT NULL,
    operation_date      date        NOT NULL,
    operation_ts        timestamptz,
    direction           text        NOT NULL CHECK (direction IN ('in', 'out')),
    amount              numeric(18, 2) NOT NULL CHECK (amount >= 0),
    amount_signed       numeric(18, 2) GENERATED ALWAYS AS
                            (CASE WHEN direction = 'in' THEN amount ELSE -amount END) STORED,
    counterparty_name   text,
    counterparty_inn    text,
    -- org | person | unknown — как в fact_operations: витрина маскирует физлиц.
    counterparty_kind   text        NOT NULL DEFAULT 'unknown',
    purpose             text,
    operation_status    text,
    raw_json            jsonb       NOT NULL,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pending_operations_account_idx
    ON finance.pending_operations (account_number);

COMMENT ON TABLE finance.pending_operations IS
    'Операции в холде (operationStatus=Authorization) — снимок на последнюю выгрузку. В ДДС и остатки не входят.';

-- Когда холды по счёту проверяли в последний раз и чем кончилось: витрине
-- нужно «на когда эти цифры», а сбой запроса не должен выглядеть как «холдов нет».
ALTER TABLE finance.sync_state ADD COLUMN IF NOT EXISTS pending_checked_at timestamptz;
ALTER TABLE finance.sync_state ADD COLUMN IF NOT EXISTS pending_error text;

-- Витрина для дашборда. Физлица маскируются так же, как в v_unclassified (008).
-- Операция, которая уже есть среди проведённых, сюда не попадает: в минуту
-- проведения она могла бы оказаться в обоих местах и посчитаться дважды.
CREATE OR REPLACE VIEW finance.v_pending AS
SELECT p.operation_date,
       right(p.account_number, 4) AS account_last4,
       p.direction,
       p.amount,
       p.amount_signed,
       CASE WHEN p.counterparty_kind = 'person' THEN 'Покупатель (физлицо)'
            ELSE p.counterparty_name END AS counterparty_name,
       CASE WHEN p.counterparty_kind = 'person'
            THEN left(coalesce(p.purpose, ''), 40)
            ELSE p.purpose END AS purpose,
       (now() AT TIME ZONE 'Europe/Moscow')::date - p.operation_date AS days_in_hold,
       p.first_seen_at,
       s.pending_checked_at AS checked_at,
       s.pending_error IS NOT NULL AS check_failed
FROM finance.pending_operations p
LEFT JOIN finance.sync_state s ON s.account_number = p.account_number
WHERE NOT EXISTS (
    SELECT 1 FROM finance.raw_operations r WHERE r.operation_id = p.operation_id
);

COMMENT ON VIEW finance.v_pending IS
    'Операции в холде для дашборда: физлица замаскированы, уже проведённые исключены.';

-- Сводка по счёту: на когда цифры и удалась ли проверка. Нужна, чтобы
-- «холдов нет» (0) отличалось от «проверить не удалось» (check_failed) и от
-- «ещё ни разу не проверяли» (checked_at пуст).
CREATE OR REPLACE VIEW finance.v_pending_summary AS
SELECT right(s.account_number, 4) AS account_last4,
       s.pending_checked_at AS checked_at,
       s.pending_error IS NOT NULL AS check_failed,
       count(p.operation_id) AS holds,
       coalesce(sum(p.amount) FILTER (WHERE p.direction = 'out'), 0) AS holds_out,
       coalesce(sum(p.amount) FILTER (WHERE p.direction = 'in'), 0) AS holds_in,
       coalesce(sum(p.amount_signed), 0) AS holds_signed
FROM finance.sync_state s
LEFT JOIN finance.pending_operations p
       ON p.account_number = s.account_number
      AND NOT EXISTS (SELECT 1 FROM finance.raw_operations r WHERE r.operation_id = p.operation_id)
GROUP BY s.account_number, s.pending_checked_at, s.pending_error;

INSERT INTO finance.schema_migrations (version)
VALUES ('009_pending_operations')
ON CONFLICT (version) DO NOTHING;
