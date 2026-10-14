"""Правила классификации: чистая логика без обращения к базе.

Порядок: ручная разметка → правила по priority (первое совпадение) → «Не разобрано».
Никаких догадок: если ничего не совпало, операция честно попадает в «Не разобрано».
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

log = logging.getLogger(__name__)

UNCLASSIFIED = "tech_unclassified"
LOAN_PAYMENT = "tech_loan_payment"
LOAN_INTEREST = "cfo_out_loan_interest"
LOAN_PRINCIPAL = "cff_loan_principal"

# На сколько дней платёж может разойтись с датой из графика.
LOAN_DATE_TOLERANCE = timedelta(days=15)
LOAN_AMOUNT_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class Rule:
    rule_id: int
    priority: int
    field: str
    match_type: str
    value: str
    direction: str | None
    amount_min: Decimal | None
    amount_max: Decimal | None
    article_id: str


@dataclass(frozen=True)
class Article:
    article_id: str
    section: str
    direction: str


@dataclass(frozen=True)
class LoanPeriod:
    loan_id: str
    period_no: int
    due_date: date
    payment_total: Decimal
    interest: Decimal
    principal: Decimal


@dataclass(frozen=True)
class RawOperation:
    operation_id: str
    account_number: str
    operation_date: date
    direction: str
    amount: Decimal
    counterparty_name: str | None
    counterparty_inn: str | None
    purpose: str | None
    tbank_category: str | None


@dataclass
class FactRow:
    operation_id: str
    split_no: int
    account_number: str
    operation_date: date
    direction: str
    amount: Decimal
    article_id: str
    classified_by: str
    rule_id: int | None
    counterparty_name: str | None
    counterparty_inn: str | None
    counterparty_kind: str
    purpose: str | None
    flags: list[str] = field(default_factory=list)


def counterparty_kind(inn: str | None) -> str:
    """Юрлицо — ИНН 10 знаков, физлицо и ИП — 12. Всё, кроме org, в витринах маскируется."""
    digits = (inn or "").strip()
    if len(digits) == 10 and digits.isdigit():
        return "org"
    if len(digits) == 12 and digits.isdigit():
        return "person"
    return "unknown"


def _field_value(op: RawOperation, name: str) -> str:
    mapping = {
        "counterparty_inn": op.counterparty_inn,
        "counterparty_name": op.counterparty_name,
        "purpose": op.purpose,
        "tbank_category": op.tbank_category,
        "direction": op.direction,
        "account": op.account_number,
    }
    return (mapping.get(name) or "").strip()


def rule_matches(rule: Rule, op: RawOperation) -> bool:
    if rule.direction and rule.direction != op.direction:
        return False
    if rule.amount_min is not None and op.amount < rule.amount_min:
        return False
    if rule.amount_max is not None and op.amount > rule.amount_max:
        return False

    haystack = _field_value(op, rule.field)
    if not haystack:
        return False

    needle = rule.value.strip()
    if rule.match_type == "equals":
        return haystack.casefold() == needle.casefold()
    if rule.match_type == "contains":
        return needle.casefold() in haystack.casefold()
    if rule.match_type == "regex":
        try:
            return re.search(needle, haystack, re.IGNORECASE) is not None
        except re.error:
            log.error("Правило %s содержит битый regex %r — пропускаю", rule.rule_id, needle)
            return False
    return False


def find_loan_period(
    schedule: list[LoanPeriod], op: RawOperation
) -> tuple[LoanPeriod | None, list[str]]:
    """Ищет платёж в графике: сумма в копейку, дата — в пределах допуска."""
    if not schedule:
        return None, ["loan_schedule_missing"]

    by_amount = [
        period
        for period in schedule
        if abs(period.payment_total - op.amount) <= LOAN_AMOUNT_TOLERANCE
        and abs(period.due_date - op.operation_date) <= LOAN_DATE_TOLERANCE
    ]
    if by_amount:
        return min(by_amount, key=lambda p: abs(p.due_date - op.operation_date)), []

    near_date = [
        period
        for period in schedule
        if abs(period.due_date - op.operation_date) <= LOAN_DATE_TOLERANCE
    ]
    if near_date:
        # Сумма разошлась с графиком — не угадываем разбивку, отправляем на разбор.
        return None, ["loan_amount_mismatch"]
    return None, ["loan_period_not_found"]


def classify_operation(
    op: RawOperation,
    rules: list[Rule],
    articles: dict[str, Article],
    overrides: dict[str, str],
    schedule: list[LoanPeriod],
) -> list[FactRow]:
    kind = counterparty_kind(op.counterparty_inn)

    def row(article_id: str, by: str, amount: Decimal, split_no: int = 1,
            rule_id: int | None = None, direction: str | None = None,
            flags: list[str] | None = None) -> FactRow:
        return FactRow(
            operation_id=op.operation_id,
            split_no=split_no,
            account_number=op.account_number,
            operation_date=op.operation_date,
            direction=direction or op.direction,
            amount=amount,
            article_id=article_id,
            classified_by=by,
            rule_id=rule_id,
            counterparty_name=op.counterparty_name,
            counterparty_inn=op.counterparty_inn,
            counterparty_kind=kind,
            purpose=op.purpose,
            flags=flags or [],
        )

    article_id: str | None = None
    classified_by = "unclassified"
    matched_rule: int | None = None

    override = overrides.get(op.operation_id)
    if override:
        article_id, classified_by = override, "manual"
    else:
        for rule in rules:
            if not rule_matches(rule, op):
                continue
            article = articles.get(rule.article_id)
            if article and article.direction != "any" and article.direction != op.direction:
                log.warning(
                    "Правило %s ведёт на статью %s с направлением %s, а операция %s — пропускаю",
                    rule.rule_id, rule.article_id, article.direction, op.direction,
                )
                continue
            article_id, classified_by, matched_rule = rule.article_id, "rule", rule.rule_id
            break

    if article_id is None:
        return [row(UNCLASSIFIED, "unclassified", op.amount)]

    if article_id == LOAN_PAYMENT:
        period, flags = find_loan_period(schedule, op)
        if period is None:
            return [row(UNCLASSIFIED, "unclassified", op.amount, flags=flags)]
        return [
            row(LOAN_INTEREST, "loan_split", period.interest, split_no=1,
                rule_id=matched_rule, direction="out"),
            row(LOAN_PRINCIPAL, "loan_split", period.principal, split_no=2,
                rule_id=matched_rule, direction="out"),
        ]

    return [row(article_id, classified_by, op.amount, rule_id=matched_rule)]
