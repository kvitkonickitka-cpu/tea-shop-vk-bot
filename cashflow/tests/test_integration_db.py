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


def test_unknown_counterparty_is_not_mislabeled_as_person(conn):
    """Карточный платёж без recipientName/recipientInn (counterparty_kind='unknown')
    не должен подписываться как «Покупатель (физлицо)» — там нет физлица, просто
    банк не прислал получателя. Полный purpose нужен, чтобы отличить такие платежи
    от настоящих неизвестных покупателей."""
    importer.import_all(conn, DATA_DIR)
    add_operation(conn, "card-1", date(2025, 2, 3), "out", "975.58",
                  purpose="Оплата в YANDEX0000TEST")
    classify.run_classify(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT counterparty_name, purpose FROM finance.v_unclassified "
                    "WHERE operation_id = 'card-1'")
        name, purpose = cur.fetchone()
    assert name is None
    assert purpose == "Оплата в YANDEX0000TEST"


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


# --- Холды (migrations/009) -------------------------------------------------------------

from cashflow import sync as sync_module  # noqa: E402
from cashflow.config import Config  # noqa: E402
from cashflow.tbank import parse_operation  # noqa: E402


def _hold(operation_id, inn, name, amount="500.00", purpose="Оплата", day="2025-03-20"):
    return parse_operation({
        "operationId": operation_id, "operationStatus": "Authorization",
        "authorizationDate": f"{day}T12:00:00+03:00", "typeOfOperation": "Debit",
        "accountAmount": amount, "recipientName": name, "recipientInn": inn, "paymentPurpose": purpose,
    }, ACCOUNT)


class FakeBank:
    def __init__(self, holds=None, fail=False, operations=None):
        self.holds, self.fail, self.operations = holds or [], fail, operations or []

    def authorizations(self, account, period_from, period_to):
        if self.fail:
            raise RuntimeError("банк недоступен")
        yield self.holds

    def statement(self, account, period_from, period_to):
        yield self.operations, []


def _real_cfg():
    return Config(tbank_token="t", tbank_api_base="https://example.test", accounts=[ACCOUNT],
                  first_day=date(2025, 1, 1), dsn=DSN)


@pytest.fixture
def holds_db(conn):
    with conn.cursor() as cur:
        cur.execute("TRUNCATE finance.pending_operations")
        cur.execute("DELETE FROM finance.sync_state")
        cur.execute("INSERT INTO finance.sync_state (account_number, synced_through) VALUES (%s, %s)",
                    (ACCOUNT, date(2025, 3, 31)))
    conn.commit()
    return conn


def _rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def test_hold_snapshot_is_replaced_and_people_are_masked(holds_db):
    bank = FakeBank([_hold("h-org", "7712345678", "ООО Хостинг", "1200.00"),
                     _hold("h-person", "771234567890", "Иванов Иван Иванович", "300.00",
                           purpose="Перевод Иванову за аренду помещения на март месяц 2025 года")])
    assert sync_module.sync_pending(holds_db, bank, _real_cfg(), ACCOUNT, date(2025, 3, 25)) == 2
    rows = _rows(holds_db, "SELECT counterparty_name, purpose, amount_signed FROM finance.v_pending ORDER BY amount")
    assert rows[0][0] == "Покупатель (физлицо)" and "Иванов" not in rows[0][0]
    assert len(rows[0][1]) <= 40
    assert rows[1][0] == "ООО Хостинг" and rows[1][2] == Decimal("-1200.00")
    summary = _rows(holds_db, "SELECT holds, holds_out, holds_signed, check_failed, checked_at IS NOT NULL "
                              "FROM finance.v_pending_summary")
    assert summary == [(2, Decimal("1500.00"), Decimal("-1500.00"), False, True)]

    # Следующий снимок — человека уже нет (провелось или отменилось): строка ушла.
    sync_module.sync_pending(holds_db, FakeBank([_hold("h-org", "7712345678", "ООО Хостинг", "1200.00")]),
                             _real_cfg(), ACCOUNT, date(2025, 3, 26))
    assert _rows(holds_db, "SELECT operation_id FROM finance.pending_operations") == [("h-org",)]
    # Пустой снимок — холдов нет совсем.
    sync_module.sync_pending(holds_db, FakeBank([]), _real_cfg(), ACCOUNT, date(2025, 3, 27))
    assert _rows(holds_db, "SELECT count(*) FROM finance.pending_operations") == [(0,)]
    assert _rows(holds_db, "SELECT holds, holds_signed FROM finance.v_pending_summary") == [(0, Decimal("0"))]


def test_repeated_snapshot_changes_nothing(holds_db):
    bank = FakeBank([_hold("h-1", "7712345678", "ООО А", "100.00")])
    sync_module.sync_pending(holds_db, bank, _real_cfg(), ACCOUNT, date(2025, 3, 25))
    before = _rows(holds_db, "SELECT operation_id, amount, first_seen_at FROM finance.pending_operations")
    sync_module.sync_pending(holds_db, bank, _real_cfg(), ACCOUNT, date(2025, 3, 25))
    assert _rows(holds_db, "SELECT operation_id, amount, first_seen_at FROM finance.pending_operations") == before


def test_settled_hold_is_not_counted_twice(holds_db):
    sync_module.sync_pending(holds_db, FakeBank([_hold("same-id", "7712345678", "ООО Б", "700.00")]),
                             _real_cfg(), ACCOUNT, date(2025, 3, 25))
    # Банк провёл операцию, а снимок холдов ещё не обновлён.
    add_operation(holds_db, "same-id", date(2025, 3, 25), "out", "700.00", name="ООО Б", inn="7712345678")
    assert _rows(holds_db, "SELECT count(*) FROM finance.v_pending") == [(0,)]
    assert _rows(holds_db, "SELECT holds, holds_signed FROM finance.v_pending_summary") == [(0, Decimal("0"))]


def test_failed_hold_request_keeps_snapshot_and_shows_failure(holds_db):
    sync_module.sync_pending(holds_db, FakeBank([_hold("h-1", "7712345678", "ООО А", "100.00")]),
                             _real_cfg(), ACCOUNT, date(2025, 3, 25))
    assert sync_module.sync_pending(holds_db, FakeBank(fail=True), _real_cfg(), ACCOUNT, date(2025, 3, 26)) is None
    assert _rows(holds_db, "SELECT operation_id FROM finance.pending_operations") == [("h-1",)]
    assert _rows(holds_db, "SELECT check_failed FROM finance.v_pending_summary") == [(True,)]
    # Следующая удачная проверка снимает отметку.
    sync_module.sync_pending(holds_db, FakeBank([]), _real_cfg(), ACCOUNT, date(2025, 3, 27))
    assert _rows(holds_db, "SELECT check_failed FROM finance.v_pending_summary") == [(False,)]


def test_main_sync_survives_failed_holds(holds_db):
    results = sync_module.run_sync(holds_db, FakeBank(fail=True), _real_cfg(),
                                   period_from=date(2025, 3, 1), period_to=date(2025, 3, 31))
    assert len(results) == 1
    assert _rows(holds_db, "SELECT last_status FROM finance.sync_state") == [("ok",)]


def test_holds_do_not_touch_cash_flow(seeded):
    before = fetch_summary(seeded, "M")
    with seeded.cursor() as cur:
        cur.execute("INSERT INTO finance.sync_state (account_number, synced_through) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", (ACCOUNT, date(2025, 3, 31)))
    seeded.commit()
    sync_module.sync_pending(seeded, FakeBank([_hold("h-big", "7712345678", "ООО В", "99999.00", day="2025-03-28")]),
                             _real_cfg(), ACCOUNT, date(2025, 3, 31))
    assert fetch_summary(seeded, "M") == before  # ДДС, остатки, check_diff — как были
    with seeded.cursor() as cur:
        cur.execute("TRUNCATE finance.pending_operations")
    seeded.commit()
