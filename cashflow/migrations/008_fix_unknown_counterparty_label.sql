-- 008: v_unclassified путала «нет данных о контрагенте» (карточные платежи без
-- recipientName/recipientInn в ответе банка, counterparty_kind = 'unknown')
-- с «контрагент — физлицо» (counterparty_kind = 'person') — обоим подписывала
-- «Покупатель (физлицо)». Из-за этого карточные расходы (Yandex Cloud, хостинг)
-- на дашборде выглядели как неизвестные поступления от покупателей.
--
-- Теперь маскируется только реальное физлицо; для unknown показываем то, что
-- есть (как правило, пусто) — скрывать нечего, раз имени и так не было.

\set ON_ERROR_STOP on

CREATE OR REPLACE VIEW finance.v_unclassified AS
SELECT f.operation_id,
       f.operation_date,
       f.account_number,
       f.direction,
       f.amount,
       CASE WHEN f.counterparty_kind = 'org' THEN f.counterparty_name
            WHEN f.counterparty_kind = 'person' THEN 'Покупатель (физлицо)'
            ELSE f.counterparty_name END AS counterparty_name,
       CASE WHEN f.counterparty_kind = 'org' THEN f.counterparty_inn END AS counterparty_inn,
       CASE WHEN f.counterparty_kind = 'person'
            THEN left(coalesce(f.purpose, ''), 40)
            ELSE f.purpose END AS purpose,
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
VALUES ('008_fix_unknown_counterparty_label')
ON CONFLICT (version) DO NOTHING;
