"""Классификация на правилах из настоящего data/rules.csv, без базы.

В отличие от test_classify.py (синтетические правила из conftest) здесь
проверяется файл, который реально загружается в прод через import-rules —
опечатка или забытое правило всплывёт здесь, а не только на дашборде.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from cashflow.engine import Article, Rule, classify_operation
from cashflow.importer import read_csv
from conftest import make_operation

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _load_real_rules_and_articles() -> tuple[list[Rule], dict[str, Article]]:
    article_rows = read_csv(DATA_DIR / "articles.csv")
    articles = {
        row["article_id"]: Article(row["article_id"], row["section"], row["direction"])
        for row in article_rows
    }

    rule_rows = read_csv(DATA_DIR / "rules.csv")

    def _decimal_or_none(value: str) -> Decimal | None:
        return Decimal(value) if value else None

    rules = [
        Rule(
            rule_id=i,
            priority=int(row["priority"]),
            field=row["field"],
            match_type=row["match_type"],
            value=row["value"],
            direction=row.get("direction") or None,
            amount_min=_decimal_or_none(row.get("amount_min", "")),
            amount_max=_decimal_or_none(row.get("amount_max", "")),
            article_id=row["article_id"],
        )
        for i, row in enumerate(rule_rows, start=1)
        if row.get("active", "true").strip().lower() in {"1", "true", "да", "yes", "y"}
    ]
    rules.sort(key=lambda r: r.priority)
    return rules, articles


def test_yandex_terminal_payment_is_direct_customer_revenue():
    """Оплаты физлиц через терминал Яндекс должны попадать в CFO-выручку, не в «Не разобрано»."""
    rules, articles = _load_real_rules_and_articles()
    op = make_operation(
        operation_id="yx-1",
        direction="in",
        amount=Decimal("5990.00"),
        operation_date=date(2026, 9, 25),
        counterparty_name="Сидоров Пётр Иванович",
        counterparty_inn="771234567890",
        purpose="Оплата в YANDEX7372OBLAKO",
    )
    rows = classify_operation(op, rules, articles, {}, [])
    assert len(rows) == 1
    assert rows[0].article_id == "cfo_in_direct"
    assert rows[0].classified_by == "rule"


def test_unrelated_yandex_cloud_expense_is_not_affected():
    """Правило про YANDEX7372OBLAKO не должно задевать расходы на Yandex Cloud (другое направление)."""
    rules, articles = _load_real_rules_and_articles()
    op = make_operation(
        operation_id="yc-1",
        direction="out",
        amount=Decimal("1200.00"),
        counterparty_name='ООО "Яндекс.Облако"',
        purpose="Подписка Yandex Cloud",
    )
    rows = classify_operation(op, rules, articles, {}, [])
    assert rows[0].article_id == "cfo_out_services"
