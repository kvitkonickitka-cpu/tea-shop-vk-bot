from datetime import date
from decimal import Decimal

from cashflow.engine import classify_operation, counterparty_kind
from conftest import make_operation


def run(op, rules, articles, overrides=None, schedule=None):
    return classify_operation(op, rules, articles, overrides or {}, schedule or [])


def test_yookassa_payout_goes_to_revenue(rules, articles):
    op = make_operation(
        operation_id="yk-1", direction="in", amount=Decimal("48250.13"),
        counterparty_name='НКО "ЮМани"', counterparty_inn="7750005725",
        purpose="Перечисление по реестру",
    )
    rows = run(op, rules, articles)
    assert len(rows) == 1
    assert rows[0].article_id == "cfo_in_yookassa"
    assert rows[0].classified_by == "rule"


def test_enp_is_single_article(rules, articles):
    op = make_operation(
        operation_id="tax-1", amount=Decimal("12000.00"),
        counterparty_name="Казначейство России",
        purpose="Единый налоговый платеж",
    )
    rows = run(op, rules, articles)
    assert [r.article_id for r in rows] == ["cfo_out_taxes"]


def test_internal_transfer_is_technical(rules, articles):
    op = make_operation(
        operation_id="tr-1", amount=Decimal("50000.00"),
        purpose="Перевод собственных средств на накопительный счет",
    )
    rows = run(op, rules, articles)
    assert rows[0].article_id == "tech_internal_transfer"


def test_owner_draw(rules, articles):
    op = make_operation(
        operation_id="ow-1", amount=Decimal("30000.00"),
        counterparty_name="Иванов Никита Сергеевич", counterparty_inn="771234567890",
        purpose="Перевод средств",
    )
    rows = run(op, rules, articles)
    assert rows[0].article_id == "cff_owner_draw_nikita"
    assert rows[0].counterparty_kind == "person"


def test_regex_rule(rules, articles):
    op = make_operation(
        operation_id="dir-1", direction="in", amount=Decimal("2400.00"),
        purpose="Оплата за Заказ № 1024",
    )
    rows = run(op, rules, articles)
    assert rows[0].article_id == "cfo_in_direct"


def test_unknown_operation_is_never_guessed(rules, articles):
    op = make_operation(
        operation_id="x-1", amount=Decimal("777.00"),
        counterparty_name="ООО Ромашка", counterparty_inn="7799999999",
        purpose="Оплата по счету 15",
    )
    rows = run(op, rules, articles)
    assert rows[0].article_id == "tech_unclassified"
    assert rows[0].classified_by == "unclassified"


def test_manual_override_wins_over_rules(rules, articles):
    op = make_operation(
        operation_id="ov-1", amount=Decimal("5000.00"),
        purpose="Единый налоговый платеж",
    )
    rows = run(op, rules, articles, overrides={"ov-1": "cfo_out_goods"})
    assert rows[0].article_id == "cfo_out_goods"
    assert rows[0].classified_by == "manual"


def test_first_matching_rule_wins(rules, articles):
    # Совпадает и правило про налоги (приоритет 20), и про поставщика (50).
    op = make_operation(
        operation_id="p-1", amount=Decimal("9000.00"), counterparty_inn="7712345678",
        purpose="Единый налоговый платеж",
    )
    rows = run(op, rules, articles)
    assert rows[0].article_id == "cfo_out_taxes"


def test_rule_with_wrong_direction_is_skipped(rules, articles):
    # Правило про ЮMoney только для прихода, а операция расходная.
    op = make_operation(
        operation_id="d-1", direction="out", amount=Decimal("100.00"),
        counterparty_name='НКО "ЮМани"',
    )
    rows = run(op, rules, articles)
    assert rows[0].article_id == "tech_unclassified"


def test_counterparty_kind():
    assert counterparty_kind("7712345678") == "org"
    assert counterparty_kind("771234567890") == "person"
    assert counterparty_kind(None) == "unknown"
    assert counterparty_kind("") == "unknown"


def test_weekend_operation_keeps_its_date(rules, articles):
    # 8 марта 2025 — суббота. Дата не должна съезжать на рабочий день.
    op = make_operation(
        operation_id="we-1", operation_date=date(2025, 3, 8), amount=Decimal("1500.00"),
        purpose="Единый налоговый платеж",
    )
    rows = run(op, rules, articles)
    assert rows[0].operation_date == date(2025, 3, 8)
