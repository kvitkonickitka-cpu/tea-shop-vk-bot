from datetime import date
from decimal import Decimal

from cashflow.engine import classify_operation
from cashflow.loans import add_months, annuity_schedule
from conftest import make_operation


def test_loan_payment_splits_into_interest_and_principal(rules, articles, loan_schedule):
    op = make_operation(
        operation_id="loan-1", operation_date=date(2025, 3, 10), amount=Decimal("9601.74"),
        purpose="Погашение кредита по договору 123",
    )
    rows = classify_operation(op, rules, articles, {}, loan_schedule)

    assert [r.article_id for r in rows] == ["cfo_out_loan_interest", "cff_loan_principal"]
    assert rows[0].amount == Decimal("2084.59")
    assert rows[1].amount == Decimal("7517.15")
    # Разбивка не должна создавать и терять деньги.
    assert rows[0].amount + rows[1].amount == op.amount
    assert all(r.classified_by == "loan_split" for r in rows)


def test_payment_a_few_days_late_still_matches(rules, articles, loan_schedule):
    op = make_operation(
        operation_id="loan-2", operation_date=date(2025, 3, 14), amount=Decimal("9601.74"),
        purpose="Погашение кредита",
    )
    rows = classify_operation(op, rules, articles, {}, loan_schedule)
    assert len(rows) == 2
    assert rows[0].amount == Decimal("2084.59")


def test_wrong_amount_is_flagged_not_guessed(rules, articles, loan_schedule):
    op = make_operation(
        operation_id="loan-3", operation_date=date(2025, 3, 10), amount=Decimal("9000.00"),
        purpose="Погашение кредита",
    )
    rows = classify_operation(op, rules, articles, {}, loan_schedule)
    assert len(rows) == 1
    assert rows[0].article_id == "tech_unclassified"
    assert "loan_amount_mismatch" in rows[0].flags


def test_missing_schedule_is_flagged(rules, articles):
    op = make_operation(
        operation_id="loan-4", amount=Decimal("9601.74"), purpose="Погашение кредита",
    )
    rows = classify_operation(op, rules, articles, {}, [])
    assert rows[0].article_id == "tech_unclassified"
    assert "loan_schedule_missing" in rows[0].flags


def test_annuity_schedule_closes_the_debt():
    schedule = annuity_schedule(Decimal("100000"), Decimal("27"), 12, date(2025, 2, 10))

    assert len(schedule) == 12
    assert schedule[0].payment_total == Decimal("9601.74")
    assert schedule[0].interest == Decimal("2250.00")
    assert schedule[-1].balance_after == Decimal("0.00")
    # Тело кредита в сумме равно выданной сумме.
    assert sum(p.principal for p in schedule) == Decimal("100000.00")
    # Каждый платёж состоит ровно из процентов и тела.
    for period in schedule:
        assert period.interest + period.principal == period.payment_total


def test_add_months_handles_short_months():
    assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2025, 12, 10), 1) == date(2026, 1, 10)
