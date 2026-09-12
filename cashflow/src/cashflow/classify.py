"""Классификация операций: чтение справочников из базы и запись результата.

Сами правила живут в engine.py и тестируются без базы.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import psycopg

from .engine import (
    UNCLASSIFIED,
    Article,
    FactRow,
    LoanPeriod,
    RawOperation,
    Rule,
    classify_operation,
)

log = logging.getLogger(__name__)


# --- работа с базой ------------------------------------------------------------------


def load_rules(conn: psycopg.Connection) -> list[Rule]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rule_id, priority, field, match_type, value, direction,
                   amount_min, amount_max, article_id
            FROM finance.rules WHERE active ORDER BY priority, rule_id
            """
        )
        return [Rule(*r) for r in cur.fetchall()]


def load_articles(conn: psycopg.Connection) -> dict[str, Article]:
    with conn.cursor() as cur:
        cur.execute("SELECT article_id, section, direction FROM finance.articles")
        return {r[0]: Article(*r) for r in cur.fetchall()}


def load_overrides(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT operation_id, article_id FROM finance.manual_overrides")
        return dict(cur.fetchall())


def load_loan_schedule(conn: psycopg.Connection) -> list[LoanPeriod]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT loan_id, period_no, due_date, payment_total, interest, principal
            FROM finance.loan_schedule ORDER BY loan_id, period_no
            """
        )
        return [LoanPeriod(*r) for r in cur.fetchall()]


def load_operations(conn: psycopg.Connection, since: date | None = None) -> list[RawOperation]:
    sql = """
        SELECT operation_id, account_number, operation_date, direction, amount,
               counterparty_name, counterparty_inn, purpose, tbank_category
        FROM finance.raw_operations
    """
    params: tuple = ()
    if since:
        sql += " WHERE operation_date >= %s"
        params = (since,)
    sql += " ORDER BY operation_date, operation_id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [RawOperation(*r) for r in cur.fetchall()]


def store_facts(conn: psycopg.Connection, rows: list[FactRow], operation_ids: list[str]) -> None:
    """Перезаписывает классификацию для перечисленных операций одной транзакцией."""
    with conn.cursor() as cur:
        if operation_ids:
            cur.execute(
                "DELETE FROM finance.fact_operations WHERE operation_id = ANY(%s)", (operation_ids,)
            )
        for row in rows:
            cur.execute(
                """
                INSERT INTO finance.fact_operations (
                    operation_id, split_no, account_number, operation_date, direction, amount,
                    article_id, classified_by, rule_id, counterparty_name, counterparty_inn,
                    counterparty_kind, purpose, flags)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row.operation_id, row.split_no, row.account_number, row.operation_date,
                    row.direction, row.amount, row.article_id, row.classified_by, row.rule_id,
                    row.counterparty_name, row.counterparty_inn, row.counterparty_kind,
                    row.purpose, row.flags,
                ),
            )
    conn.commit()


@dataclass
class ClassifyReport:
    operations: int = 0
    rows: int = 0
    manual: int = 0
    by_rule: int = 0
    loan_split: int = 0
    unclassified: int = 0
    unclassified_amount: Decimal = Decimal("0")
    flagged: list[tuple[str, list[str]]] = field(default_factory=list)


def run_classify(conn: psycopg.Connection, since: date | None = None) -> ClassifyReport:
    rules = load_rules(conn)
    articles = load_articles(conn)
    overrides = load_overrides(conn)
    schedule = load_loan_schedule(conn)
    operations = load_operations(conn, since)

    if UNCLASSIFIED not in articles:
        raise RuntimeError(
            "В справочнике нет статьи «Не разобрано». Сначала выполните import-rules."
        )

    report = ClassifyReport(operations=len(operations))
    all_rows: list[FactRow] = []
    for op in operations:
        rows = classify_operation(op, rules, articles, overrides, schedule)
        all_rows.extend(rows)
        for row in rows:
            report.rows += 1
            if row.classified_by == "manual":
                report.manual += 1
            elif row.classified_by == "rule":
                report.by_rule += 1
            elif row.classified_by == "loan_split":
                report.loan_split += 1
            else:
                report.unclassified += 1
                report.unclassified_amount += row.amount
            if row.flags:
                report.flagged.append((row.operation_id, row.flags))

    store_facts(conn, all_rows, [op.operation_id for op in operations])
    log.info(
        "Классификация: операций %s, строк %s, по правилам %s, вручную %s, "
        "разбивка кредита %s, не разобрано %s",
        report.operations, report.rows, report.by_rule, report.manual,
        report.loan_split, report.unclassified,
    )
    return report
