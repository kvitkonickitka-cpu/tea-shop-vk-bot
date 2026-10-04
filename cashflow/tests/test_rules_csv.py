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


def test_yandex_cloud_card_payment_is_services_expense():
    """Карточная оплата Yandex Cloud (без имени получателя в выписке) — расход на сервисы,
    а не выручка: это списание со счёта (direction=out), хотя из-за отсутствия
    recipientName/recipientInn она выглядит в сырых данных как «неизвестный контрагент».
    """
    rules, articles = _load_real_rules_and_articles()
    op = make_operation(
        operation_id="yx-1",
        direction="out",
        amount=Decimal("975.58"),
        operation_date=date(2026, 9, 25),
        counterparty_name=None,
        counterparty_inn=None,
        purpose="Оплата в YANDEX*7372*OBLAKO Moskva RUS",
    )
    rows = classify_operation(op, rules, articles, {}, [])
    assert len(rows) == 1
    assert rows[0].article_id == "cfo_out_services"
    assert rows[0].classified_by == "rule"


def test_aeza_hosting_card_payment_is_services_expense():
    rules, articles = _load_real_rules_and_articles()
    op = make_operation(
        operation_id="az-1",
        direction="out",
        amount=Decimal("630.00"),
        operation_date=date(2026, 10, 5),
        counterparty_name=None,
        counterparty_inn=None,
        purpose="YM*aeza grupp",
    )
    rows = classify_operation(op, rules, articles, {}, [])
    assert rows[0].article_id == "cfo_out_services"


def test_incoming_payment_with_same_text_is_not_guessed_as_revenue():
    """На всякий случай: правила по purpose ограничены direction=out, входящую операцию
    с тем же текстом назначения они не должны трогать (а не угадывать статью)."""
    rules, articles = _load_real_rules_and_articles()
    op = make_operation(
        operation_id="in-1",
        direction="in",
        amount=Decimal("975.58"),
        purpose="Оплата в YANDEX*7372*OBLAKO Moskva RUS",
    )
    rows = classify_operation(op, rules, articles, {}, [])
    assert rows[0].article_id == "tech_unclassified"
