import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cashflow.engine import Article, LoanPeriod, RawOperation, Rule  # noqa: E402


@pytest.fixture
def articles() -> dict[str, Article]:
    return {
        "cfo_in_yookassa": Article("cfo_in_yookassa", "CFO", "in"),
        "cfo_in_direct": Article("cfo_in_direct", "CFO", "in"),
        "cfo_out_goods": Article("cfo_out_goods", "CFO", "out"),
        "cfo_out_taxes": Article("cfo_out_taxes", "CFO", "out"),
        "cfo_out_loan_interest": Article("cfo_out_loan_interest", "CFO", "out"),
        "cff_loan_principal": Article("cff_loan_principal", "CFF", "out"),
        "cff_owner_draw_nikita": Article("cff_owner_draw_nikita", "CFF", "out"),
        "tech_internal_transfer": Article("tech_internal_transfer", "TECH", "any"),
        "tech_loan_payment": Article("tech_loan_payment", "TECH", "out"),
        "tech_unclassified": Article("tech_unclassified", "TECH", "any"),
    }


@pytest.fixture
def rules() -> list[Rule]:
    return [
        Rule(1, 10, "purpose", "contains", "перевод собственных средств", None, None, None,
             "tech_internal_transfer"),
        Rule(2, 20, "purpose", "contains", "единый налоговый платеж", "out", None, None,
             "cfo_out_taxes"),
        Rule(3, 30, "purpose", "contains", "погашение кредита", "out", None, None,
             "tech_loan_payment"),
        Rule(4, 40, "counterparty_name", "contains", "юмани", "in", None, None,
             "cfo_in_yookassa"),
        Rule(5, 50, "counterparty_inn", "equals", "7712345678", "out", None, None,
             "cfo_out_goods"),
        Rule(6, 60, "counterparty_name", "contains", "иванов никита", "out", None, None,
             "cff_owner_draw_nikita"),
        Rule(7, 70, "purpose", "regex", r"заказ\s*№\s*\d+", "in", None, None, "cfo_in_direct"),
    ]


@pytest.fixture
def loan_schedule() -> list[LoanPeriod]:
    """Реальный аннуитет: 100 000 ₽, 27% годовых, 12 месяцев."""
    return [
        LoanPeriod("main", 1, date(2025, 2, 10), Decimal("9601.74"), Decimal("2250.00"),
                   Decimal("7351.74")),
        LoanPeriod("main", 2, date(2025, 3, 10), Decimal("9601.74"), Decimal("2084.59"),
                   Decimal("7517.15")),
        LoanPeriod("main", 3, date(2025, 4, 10), Decimal("9601.74"), Decimal("1915.45"),
                   Decimal("7686.29")),
    ]


def make_operation(**kwargs) -> RawOperation:
    defaults = dict(
        operation_id="op-1",
        account_number="40802810100000000001",
        operation_date=date(2025, 3, 10),
        direction="out",
        amount=Decimal("1000.00"),
        counterparty_name=None,
        counterparty_inn=None,
        purpose=None,
        tbank_category=None,
    )
    defaults.update(kwargs)
    return RawOperation(**defaults)
