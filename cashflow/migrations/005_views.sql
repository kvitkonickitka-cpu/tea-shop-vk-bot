-- 005: витрины. Только они доступны роли datalens_ro.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- Дневные остатки по каждому счёту.
--
-- Банк отдаёт остаток на конкретную дату (якорь). Остаток на любой другой день
-- восстанавливается движением от якоря: назад — вычитаем операции, вперёд — прибавляем.
-- Календарь общий для всех счетов, чтобы суммарный остаток по дню был полным.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW finance.v_daily_balance AS
WITH anchor AS (
    SELECT DISTINCT ON (account_number)
           account_number,
           as_of_date AS anchor_date,
           amount     AS anchor_amount
    FROM finance.balances
    WHERE balance_kind = 'closing'
    ORDER BY account_number, as_of_date DESC
),
span AS (
    -- Календарь начинается с начала года первой операции минус день: иначе
    -- у первого месяца/квартала/года не из чего взять остаток на начало периода.
    SELECT (date_trunc('year', LEAST(
               COALESCE((SELECT MIN(operation_date) FROM finance.raw_operations), CURRENT_DATE),
               COALESCE((SELECT MIN(anchor_date) FROM anchor), CURRENT_DATE)
           )) - interval '1 day')::date AS day_from,
           GREATEST(
               COALESCE((SELECT MAX(operation_date) FROM finance.raw_operations), CURRENT_DATE),
               COALESCE((SELECT MAX(anchor_date) FROM anchor), CURRENT_DATE)
           ) AS day_to
),
calendar AS (
    SELECT a.account_number, a.anchor_date, a.anchor_amount, d::date AS day
    FROM anchor a
    CROSS JOIN span s
    CROSS JOIN LATERAL generate_series(s.day_from, s.day_to, interval '1 day') d
)
SELECT c.account_number,
       c.day,
       c.anchor_amount
         - COALESCE((SELECT SUM(o.amount_signed)
                     FROM finance.raw_operations o
                     WHERE o.account_number = c.account_number
                       AND o.operation_date >  c.day
                       AND o.operation_date <= c.anchor_date), 0)
         + COALESCE((SELECT SUM(o.amount_signed)
                     FROM finance.raw_operations o
                     WHERE o.account_number = c.account_number
                       AND o.operation_date >  c.anchor_date
                       AND o.operation_date <= c.day), 0) AS balance
FROM calendar c;

COMMENT ON VIEW finance.v_daily_balance IS
    'Остаток на конец каждого дня по каждому счёту, восстановленный от банковского якоря.';

CREATE OR REPLACE VIEW finance.v_daily_balance_total AS
SELECT day, SUM(balance) AS balance
FROM finance.v_daily_balance
GROUP BY day;

-- ---------------------------------------------------------------------------
-- Календарь периодов: месяц, квартал, год.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW finance.v_periods AS
WITH span AS (
    SELECT COALESCE(MIN(operation_date), CURRENT_DATE) AS d_from,
           COALESCE(MAX(operation_date), CURRENT_DATE) AS d_to
    FROM finance.raw_operations
)
SELECT 'M'::text AS period_type,
       g::date AS period_start,
       (g + interval '1 month' - interval '1 day')::date AS period_end
FROM span, generate_series(date_trunc('month', span.d_from), span.d_to, interval '1 month') g
UNION ALL
SELECT 'Q',
       g::date,
       (g + interval '3 months' - interval '1 day')::date
FROM span, generate_series(date_trunc('quarter', span.d_from), span.d_to, interval '3 months') g
UNION ALL
SELECT 'Y',
       g::date,
       (g + interval '1 year' - interval '1 day')::date
FROM span, generate_series(date_trunc('year', span.d_from), span.d_to, interval '1 year') g;

-- ---------------------------------------------------------------------------
-- Потоки по периодам.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW finance.v_flows AS
SELECT p.period_type,
       p.period_start,
       p.period_end,
       -- Операционный поток
       COALESCE(SUM(f.amount_signed) FILTER (WHERE a.section = 'CFO' AND f.direction = 'in'),  0) AS cfo_in,
       COALESCE(SUM(f.amount_signed) FILTER (WHERE a.section = 'CFO' AND f.direction = 'out'), 0) AS cfo_out,
       COALESCE(SUM(f.amount_signed) FILTER (WHERE a.section = 'CFO'), 0) AS cfo,
       -- Инвестиции: расходы отрицательные, поэтому capex сам по себе со знаком «−»
       COALESCE(SUM(f.amount_signed) FILTER (WHERE a.section = 'CFI'), 0) AS capex,
       -- Финансирование
       COALESCE(SUM(f.amount_signed) FILTER (WHERE f.article_id = 'cff_loan_received'), 0)     AS loan_in,
       COALESCE(SUM(f.amount_signed) FILTER (WHERE f.article_id = 'cff_loan_principal'), 0)    AS loan_principal_out,
       COALESCE(SUM(f.amount_signed) FILTER (WHERE f.article_id = 'cff_owner_contribution'), 0) AS owner_in,
       COALESCE(SUM(f.amount_signed) FILTER (WHERE f.article_id IN
           ('cff_owner_draw_nikita', 'cff_owner_draw_ilya')), 0)                               AS owner_out,
       -- Технические движения: переводы между своими счетами в сумме дают ноль
       COALESCE(SUM(f.amount_signed) FILTER (WHERE a.section = 'TECH'), 0) AS tech_net,
       -- Изменение денег за период по всем счетам
       COALESCE(SUM(f.amount_signed), 0) AS net_change,
       -- Проценты по кредиту нужны отдельно для DSCR
       COALESCE(SUM(f.amount_signed) FILTER (WHERE f.article_id = 'cfo_out_loan_interest'), 0) AS loan_interest_out,
       COALESCE(SUM(f.amount_signed) FILTER (WHERE f.article_id = 'cfo_out_taxes'), 0)         AS taxes_out,
       COALESCE(SUM(f.amount_signed) FILTER (WHERE a.grp = 'Маркетинг'), 0)                    AS marketing_out,
       -- Доля «Не разобрано»
       COALESCE(SUM(f.amount) FILTER (WHERE f.article_id = 'tech_unclassified'), 0)            AS unclassified_amount,
       COALESCE(SUM(f.amount), 0)                                                              AS total_amount,
       COUNT(*) FILTER (WHERE f.article_id = 'tech_unclassified')                              AS unclassified_count,
       COUNT(f.operation_id)                                                                   AS operations_count
FROM finance.v_periods p
LEFT JOIN finance.fact_operations f
       ON f.operation_date BETWEEN p.period_start AND p.period_end
LEFT JOIN finance.articles a ON a.article_id = f.article_id
GROUP BY p.period_type, p.period_start, p.period_end;

-- ---------------------------------------------------------------------------
-- Главная витрина: деньги за период.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW finance.v_cash_summary AS
WITH base AS (
    SELECT fl.*,
           -- Остатки берутся из дневных остатков, а не суммируются между периодами.
           -- Если банк ещё не присылал остатков, здесь будет NULL — «неизвестно»,
           -- а не ноль: ложное расхождение хуже отсутствия цифры.
           (SELECT balance FROM finance.v_daily_balance_total
            WHERE day = fl.period_start - 1) AS opening_balance,
           (SELECT balance FROM finance.v_daily_balance_total
            WHERE day = LEAST(fl.period_end,
                              (SELECT MAX(day) FROM finance.v_daily_balance_total))) AS closing_balance,
           (SELECT MIN(balance) FROM finance.v_daily_balance_total
            WHERE day BETWEEN fl.period_start AND fl.period_end) AS min_balance
    FROM finance.v_flows fl
)
SELECT period_type,
       period_start,
       period_end,
       opening_balance,
       closing_balance,
       min_balance,
       cfo_in,
       cfo_out,
       cfo,
       capex,
       cfo + capex AS fcf,
       loan_in,
       loan_principal_out,
       cfo + capex + loan_in + loan_principal_out AS fcfe,
       owner_in,
       owner_out,
       tech_net,
       net_change,
       -- Контроль: остаток на конец минус остаток на начало должен совпасть с движением
       round(closing_balance - opening_balance - net_change, 2) AS check_diff,
       CASE WHEN total_amount > 0
            THEN round(unclassified_amount / total_amount, 4) ELSE 0 END AS unclassified_share_amount,
       CASE WHEN operations_count > 0
            THEN round(unclassified_count::numeric / operations_count, 4) ELSE 0 END AS unclassified_share_count,
       CASE WHEN period_type = 'M'
            THEN SUM(cfo + capex) OVER (PARTITION BY period_type
                                        ORDER BY period_start
                                        ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)
       END AS fcf_rolling_3m,
       loan_interest_out,
       taxes_out,
       marketing_out,
       operations_count
FROM base;

COMMENT ON VIEW finance.v_cash_summary IS
    'Денежный поток за месяц/квартал/год. check_diff обязан быть равен 0.';

-- ---------------------------------------------------------------------------
-- Метрики.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW finance.v_metrics AS
WITH s AS (SELECT * FROM finance.v_cash_summary),
settings AS (
    SELECT (SELECT value::numeric FROM finance.settings WHERE key = 'runway_months') AS runway_months,
           (SELECT value::numeric FROM finance.settings WHERE key = 'contrib_1pct_threshold') AS contrib_threshold,
           (SELECT value::numeric FROM finance.settings WHERE key = 'vat_threshold_usn') AS vat_threshold
),
avg_burn AS (
    -- Средний операционный отток за последние N месяцев, положительное число
    SELECT period_start,
           AVG(-cfo_out) OVER (ORDER BY period_start ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS burn
    FROM s WHERE period_type = 'M'
),
ytd AS (
    SELECT period_start,
           SUM(cfo_in) OVER (PARTITION BY date_trunc('year', period_start)
                             ORDER BY period_start) AS income_ytd
    FROM s WHERE period_type = 'M'
)
SELECT s.period_type,
       s.period_start,
       s.closing_balance,
       s.cfo,
       s.fcf,
       -- Сколько остаётся с рубля поступлений
       CASE WHEN s.cfo_in > 0 THEN round(s.cfo / s.cfo_in, 4) END AS cfo_margin,
       -- На сколько месяцев хватит денег при текущем оттоке
       CASE WHEN b.burn > 0 THEN round(s.closing_balance / b.burn, 2) END AS runway_months,
       -- Обслуживание долга: (операционный поток + проценты) / (проценты + тело)
       CASE WHEN (-s.loan_interest_out - s.loan_principal_out) > 0
            THEN round((s.cfo - s.loan_interest_out) / (-s.loan_interest_out - s.loan_principal_out), 2)
       END AS dscr,
       CASE WHEN s.fcfe > 0 THEN round(-s.owner_out / s.fcfe, 4) END AS owner_draw_share,
       CASE WHEN s.cfo_in > 0 THEN round(-s.taxes_out / s.cfo_in, 4) END AS tax_share,
       CASE WHEN s.cfo_in > 0 THEN round(-s.marketing_out / s.cfo_in, 4) END AS marketing_share,
       y.income_ytd,
       y.income_ytd > st.contrib_threshold AS contrib_1pct_reached,
       y.income_ytd > st.vat_threshold     AS vat_threshold_reached,
       s.unclassified_share_amount
FROM s
CROSS JOIN settings st
LEFT JOIN avg_burn b ON b.period_start = s.period_start AND s.period_type = 'M'
LEFT JOIN ytd y      ON y.period_start = s.period_start AND s.period_type = 'M';

-- ---------------------------------------------------------------------------
-- Доли поступлений по каналам и доли групп расходов.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW finance.v_channel_mix AS
WITH monthly AS (
    SELECT date_trunc('month', f.operation_date)::date AS period_start,
           a.article AS channel,
           SUM(f.amount) AS amount
    FROM finance.fact_operations f
    JOIN finance.articles a ON a.article_id = f.article_id
    WHERE a.section = 'CFO' AND f.direction = 'in'
    GROUP BY 1, 2
)
SELECT period_start,
       channel,
       amount,
       round(amount / NULLIF(SUM(amount) OVER (PARTITION BY period_start), 0), 4) AS share
FROM monthly;

-- ---------------------------------------------------------------------------
-- Таблица ДДС: статья × месяц.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW finance.v_ddc AS
SELECT date_trunc('month', f.operation_date)::date AS period_start,
       a.section,
       a.grp,
       a.article_id,
       a.article,
       SUM(f.amount_signed) AS amount,
       COUNT(*) AS operations_count
FROM finance.fact_operations f
JOIN finance.articles a ON a.article_id = f.article_id
GROUP BY 1, 2, 3, 4, 5;

-- ---------------------------------------------------------------------------
-- Что требует внимания: «Не разобрано» и расхождения.
-- Контрагенты-физлица маскируются.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW finance.v_unclassified AS
SELECT f.operation_id,
       f.operation_date,
       f.account_number,
       f.direction,
       f.amount,
       CASE WHEN f.counterparty_kind = 'org'
            THEN f.counterparty_name
            ELSE 'Покупатель (физлицо)' END AS counterparty_name,
       CASE WHEN f.counterparty_kind = 'org' THEN f.counterparty_inn END AS counterparty_inn,
       CASE WHEN f.counterparty_kind = 'org' THEN f.purpose
            ELSE left(coalesce(f.purpose, ''), 40) END AS purpose,
       f.article_id,
       f.flags,
       'operation'::text AS issue_kind,
       CASE WHEN f.article_id = 'tech_unclassified' THEN 'Не разобрано'
            ELSE array_to_string(f.flags, ', ') END AS issue
FROM finance.fact_operations f
WHERE f.article_id = 'tech_unclassified' OR cardinality(f.flags) > 0
UNION ALL
-- Месяцы, где остаток не сходится с движением денег
SELECT NULL, s.period_start, NULL, NULL, s.check_diff,
       NULL, NULL, NULL, NULL, ARRAY['check_diff'],
       'period', 'Остаток не сходится: check_diff = ' || s.check_diff
FROM finance.v_cash_summary s
WHERE s.period_type = 'M' AND s.check_diff <> 0;

INSERT INTO finance.schema_migrations (version)
VALUES ('005_views')
ON CONFLICT (version) DO NOTHING;
