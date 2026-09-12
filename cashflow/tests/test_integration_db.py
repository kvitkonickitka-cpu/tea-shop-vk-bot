"""Проверки на настоящей базе: сходимость денег, идемпотентность, права DataLens.

Запускаются только если задана переменная CASHFLOW_TEST_DSN, и только против базы,
в имени которой есть «test» — чтобы тест физически не мог стереть боевые данные.

    createdb finance_test
    psql -d finance_test -v sync_password=... -v ro_password=... -f migrations/001_schema_and_roles.sql
    CASHFLOW_TEST_DSN="host=/tmp port=5432 dbname=finance_test user=finance_sync password=..." \
        pytest tests/test_integration_db.py
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from cashflow import classify, db, importer  # noqa: E402

DSN = os.environ.get("CASHFLOW_TEST_DSN", "")
DATA_DIR = Path(__file__).resolve().parents[1] / "data"

pytestmark = pytest.mark.skipif(not DSN, reason="CASHFLOW_TEST_DSN не задан")

ACCOUNT = "40802810100000000001"


def _guard(dsn: str) -> None:
    if "test" not in dsn.lower():
        pytest.skip("Тест работает только с базой, в имени которой есть «test»")


@pytest.fixture
def conn():
    _guard(DSN)
    connection = db.connect(DSN)
    db.apply_migrations(connection)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE finance.fact_operations, finance.raw_operations, finance.balances")
    connection.commit()
    yield connection
    connection.close()


def add_operation(conn, operation_id, day, direction, amount, **fields):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finance.raw_operations (
                operation_id, account_number, operation_date, direction, amount,
                counterparty_name, counterparty_inn, purpose, operation_status, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Transaction', %s)
            ON CONFLICT (operation_id) DO UPDATE SET amount = EXCLUDED.amount
            """,
            (
                operation_id, ACCOUNT, day, direction, Decimal(amount),
                fields.get("name"), fields.get("inn"), fields.get("purpose"),
                json.dumps({"operationId": operation_id}),
            ),
        )
    conn.commit()


def set_closing_balance(conn, day, amount):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finance.balances (account_number, as_of_date, balance_kind, amount)
            VALUES (%s, %s, 'closing', %s)
            ON CONFLICT (account_number, as_of_date, balance_kind)
            DO UPDATE SET amount = EXCLUDED.amount
            """,
            (ACCOUNT, day, Decimal(amount)),
        )
    conn.commit()


@pytest.fixture
def seeded(conn):
    """Два месяца операций: выручка, закупка, налоги, платёж по кредиту, вывод денег."""
    importer.import_all(conn, DATA_DIR)

    add_operation(conn, "f-1", date(2025, 2, 5), "in", "50000.00",
                  name='НКО "ЮМани"', inn="7750005725", purpose="Перечисление по реестру")
    add_operation(conn, "f-2", date(2025, 2, 12), "out", "20000.00",
                  purpose="Оплата упаковки для чая")
    add_operation(conn, "f-3", date(2025, 2, 10), "out", "9601.74",
                  purpose="Погашение кредита по договору 1")
    add_operation(conn, "f-4", date(2025, 3, 6), "in", "80000.00",
                  name='НКО "ЮМани"', inn="7750005725", purpose="Перечисление по реестру")
    add_operation(conn, "f-5", date(2025, 3, 11), "out", "12000.00",
                  name="Казначейство России", purpose="Единый налоговый платеж")
    add_operation(conn, "f-6", date(2025, 3, 20), "out", "1234.00",
                  name="ООО Неизвестный", inn="7799999999", purpose="Оплата по счету 7")

    # Остаток на конец марта: банк сказал столько.
    set_closing_balance(conn, date(2025, 3, 31), "87164.26")
    classify.run_classify(conn)
    return conn


def fetch_summary(conn, period_type: str) -> dict[date, dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT period_start, opening_balance, closing_balance, min_balance, cfo, capex,
                   fcf, fcfe, net_change, check_diff, unclassified_share_amount
            FROM finance.v_cash_summary WHERE period_type = %s ORDER BY period_start
            """,
            (period_type,),
        )
        columns = [d.name for d in cur.description]
        return {row[0]: dict(zip(columns, row)) for row in cur.fetchall()}


def test_check_diff_is_zero_every_month(seeded):
    months = fetch_summary(seeded, "M")
    assert months, "витрина пуста"
    for period_start, row in months.items():
        assert row["check_diff"] == Decimal("0.00"), f"{period_start}: {row['check_diff']}"


def test_balances_are_not_summed_between_months(seeded):
    months = fetch_summary(seeded, "M")
    february, march = months[date(2025, 2, 1)], months[date(2025, 3, 1)]
    # Остаток на конец февраля обязан быть остатком на начало марта.
    assert february["closing_balance"] == march["opening_balance"]
    assert march["closing_balance"] == Decimal("87164.26")


def test_quarter_flows_equal_sum_of_months(seeded):
    months = fetch_summary(seeded, "M")
    quarters = fetch_summary(seeded, "Q")
    q1 = quarters[date(2025, 1, 1)]

    assert q1["fcf"] == sum(m["fcf"] for m in months.values())
    assert q1["cfo"] == sum(m["cfo"] for m in months.values())
    assert q1["net_change"] == sum(m["net_change"] for m in months.values())
    # Остаток за квартал — на последний день, а не сумма остатков месяцев.
    assert q1["closing_balance"] == months[date(2025, 3, 1)]["closing_balance"]


def test_loan_payment_is_split_between_sections(seeded):
    with seeded.cursor() as cur:
        cur.execute(
            "SELECT article_id, amount FROM finance.fact_operations "
            "WHERE operation_id = 'f-3' ORDER BY split_no"
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["cfo_out_loan_interest", "cff_loan_principal"]
    assert sum(r[1] for r in rows) == Decimal("9601.74")


def test_reclassify_is_idempotent(seeded):
    before = fetch_summary(seeded, "M")
    with seeded.cursor() as cur:
        cur.execute("SELECT count(*), sum(amount) FROM finance.fact_operations")
        counts_before = cur.fetchone()

    classify.run_classify(seeded)

    with seeded.cursor() as cur:
        cur.execute("SELECT count(*), sum(amount) FROM finance.fact_operations")
        assert cur.fetchone() == counts_before
    assert fetch_summary(seeded, "M") == before


def test_reimport_of_same_operation_does_not_duplicate(seeded):
    with seeded.cursor() as cur:
        cur.execute("SELECT count(*), sum(amount_signed) FROM finance.raw_operations")
        before = cur.fetchone()

    # Банк отдал ту же операцию повторно — так бывает при перекрытии периодов.
    add_operation(seeded, "f-1", date(2025, 2, 5), "in", "50000.00",
                  name='НКО "ЮМани"', inn="7750005725", purpose="Перечисление по реестру")

    with seeded.cursor() as cur:
        cur.execute("SELECT count(*), sum(amount_signed) FROM finance.raw_operations")
        assert cur.fetchone() == before


def test_unclassified_operation_is_visible(seeded):
    with seeded.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM finance.v_unclassified WHERE article_id = 'tech_unclassified'"
        )
        row = cur.fetchone()
    assert row and row[0] == 1


def test_person_counterparty_is_masked_in_views(conn):
    importer.import_all(conn, DATA_DIR)
    add_operation(conn, "p-1", date(2025, 2, 3), "in", "2500.00",
                  name="Сидоров Пётр Иванович", inn="771234567890", purpose="Перевод за чай")
    classify.run_classify(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT counterparty_name, counterparty_inn FROM finance.v_unclassified "
                    "WHERE operation_id = 'p-1'")
        name, inn = cur.fetchone()
    assert name == "Покупатель (физлицо)"
    assert inn is None


DATALENS_DSN = os.environ.get("CASHFLOW_TEST_DSN_RO", "")


@pytest.mark.skipif(not DATALENS_DSN, reason="CASHFLOW_TEST_DSN_RO не задан")
@pytest.mark.parametrize(
    "relation",
    ["finance.raw_operations", "finance.fact_operations", "finance.rules", "finance.balances"],
)
def test_datalens_role_cannot_read_raw_tables(relation):
    _guard(DATALENS_DSN)
    with psycopg.connect(DATALENS_DSN) as ro_conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with ro_conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {relation} LIMIT 1")


@pytest.mark.skipif(not DATALENS_DSN, reason="CASHFLOW_TEST_DSN_RO не задан")
@pytest.mark.parametrize(
    "view",
    ["finance.v_cash_summary", "finance.v_metrics", "finance.v_ddc", "finance.v_unclassified"],
)
def test_datalens_role_can_read_views(view):
    _guard(DATALENS_DSN)
    with psycopg.connect(DATALENS_DSN) as ro_conn, ro_conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {view} LIMIT 1")
