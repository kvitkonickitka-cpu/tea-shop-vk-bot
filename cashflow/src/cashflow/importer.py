"""Загрузка справочников из CSV в базу.

Всё или ничего: если хоть одна строка не прошла проверку, база остаётся как была,
а в лог попадает список ошибок. Так кривое правило не ломает вчерашние отчёты.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)

SECTIONS = {"CFO", "CFI", "CFF", "TECH"}
DIRECTIONS = {"in", "out", "any"}
RULE_FIELDS = {
    "counterparty_inn", "counterparty_name", "purpose",
    "tbank_category", "direction", "account",
}
MATCH_TYPES = {"equals", "contains", "regex"}


class ImportError_(RuntimeError):
    """Ошибки валидации справочников."""


@dataclass
class ImportReport:
    articles: int = 0
    rules: int = 0
    overrides: int = 0
    loan_periods: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def read_csv(path: Path) -> list[dict[str, str]]:
    """Читает CSV, пропуская строки-комментарии (начинаются с #) и пустые."""
    if not path.exists():
        raise ImportError_(f"Файл не найден: {path}")
    text = path.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "да", "yes", "y"}


def _decimal_or_none(value: str, where: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ImportError_(f"{where}: «{value}» не число") from exc


def _decimal(value: str, where: str) -> Decimal:
    result = _decimal_or_none(value, where)
    if result is None:
        raise ImportError_(f"{where}: пустое число")
    return result


def validate_articles(rows: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        article_id = row.get("article_id", "")
        if not article_id:
            errors.append(f"articles.csv строка {i}: пустой article_id")
            continue
        if article_id in seen:
            errors.append(f"articles.csv строка {i}: article_id «{article_id}» повторяется")
        seen.add(article_id)
        if row.get("section") not in SECTIONS:
            errors.append(
                f"articles.csv строка {i}: section «{row.get('section')}» — "
                f"допустимо {sorted(SECTIONS)}"
            )
        if row.get("direction") not in DIRECTIONS:
            errors.append(
                f"articles.csv строка {i}: direction «{row.get('direction')}» — "
                f"допустимо {sorted(DIRECTIONS)}"
            )
    for required in ("tech_unclassified", "tech_loan_payment", "cfo_out_loan_interest",
                     "cff_loan_principal"):
        if required not in seen:
            errors.append(f"articles.csv: обязательная статья «{required}» отсутствует")
    return errors


def validate_rules(rows: list[dict[str, str]], known_articles: set[str]) -> list[str]:
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):
        where = f"rules.csv строка {i}"
        if row.get("field") not in RULE_FIELDS:
            errors.append(f"{where}: field «{row.get('field')}» — допустимо {sorted(RULE_FIELDS)}")
        if row.get("match_type") not in MATCH_TYPES:
            errors.append(f"{where}: match_type «{row.get('match_type')}»")
        if not row.get("value"):
            errors.append(f"{where}: пустое значение value")
        article_id = row.get("article_id", "")
        if article_id not in known_articles:
            errors.append(f"{where}: неизвестная статья «{article_id}»")
        direction = row.get("direction", "")
        if direction and direction not in {"in", "out"}:
            errors.append(f"{where}: direction «{direction}» — допустимо in, out или пусто")
        if row.get("match_type") == "regex":
            try:
                re.compile(row.get("value", ""))
            except re.error as exc:
                errors.append(f"{where}: битый regex «{row.get('value')}» — {exc}")
        try:
            _decimal_or_none(row.get("amount_min", ""), where)
            _decimal_or_none(row.get("amount_max", ""), where)
        except ImportError_ as exc:
            errors.append(str(exc))
        try:
            int(row.get("priority", ""))
        except ValueError:
            errors.append(f"{where}: priority «{row.get('priority')}» не целое число")
    return errors


def validate_loan_schedule(rows: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):
        where = f"loan_schedule.csv строка {i}"
        try:
            total = _decimal(row.get("payment_total", ""), where)
            interest = _decimal(row.get("interest", ""), where)
            principal = _decimal(row.get("principal", ""), where)
        except ImportError_ as exc:
            errors.append(str(exc))
            continue
        if abs(total - interest - principal) > Decimal("0.01"):
            errors.append(
                f"{where}: {interest} + {principal} не равно платежу {total}"
            )
        try:
            date.fromisoformat(row.get("due_date", ""))
        except ValueError:
            errors.append(f"{where}: due_date «{row.get('due_date')}» не дата ГГГГ-ММ-ДД")
    return errors


def import_all(conn: psycopg.Connection, data_dir: Path) -> ImportReport:
    report = ImportReport()

    articles = read_csv(data_dir / "articles.csv")
    rules = read_csv(data_dir / "rules.csv")
    overrides = read_csv(data_dir / "manual_overrides.csv")
    loan = read_csv(data_dir / "loan_schedule.csv")

    known_articles = {row.get("article_id", "") for row in articles}

    report.errors += validate_articles(articles)
    report.errors += validate_rules(rules, known_articles)
    report.errors += validate_loan_schedule(loan)
    for i, row in enumerate(overrides, start=2):
        if row.get("article_id") not in known_articles:
            report.errors.append(
                f"manual_overrides.csv строка {i}: неизвестная статья «{row.get('article_id')}»"
            )
        if not row.get("operation_id"):
            report.errors.append(f"manual_overrides.csv строка {i}: пустой operation_id")

    if report.errors:
        log.error(
            "Справочники не загружены, в базе остались прежние значения. Ошибок: %s",
            len(report.errors),
        )
        for error in report.errors:
            log.error("  %s", error)
        return report

    with conn.cursor() as cur:
        for row in articles:
            cur.execute(
                """
                INSERT INTO finance.articles
                    (article_id, article, section, direction, grp, is_capex, comment)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (article_id) DO UPDATE SET
                    article = EXCLUDED.article, section = EXCLUDED.section,
                    direction = EXCLUDED.direction, grp = EXCLUDED.grp,
                    is_capex = EXCLUDED.is_capex, comment = EXCLUDED.comment
                """,
                (
                    row["article_id"], row.get("article", ""), row["section"], row["direction"],
                    row.get("grp") or None, _bool(row.get("is_capex", "")), row.get("comment") or None,
                ),
            )
        report.articles = len(articles)

        # Правила пересоздаются целиком: так удалённая строка в CSV исчезает и из базы.
        cur.execute("DELETE FROM finance.rules")
        for row in rules:
            cur.execute(
                """
                INSERT INTO finance.rules
                    (priority, field, match_type, value, direction, amount_min, amount_max,
                     article_id, active, comment)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    int(row["priority"]), row["field"], row["match_type"], row["value"],
                    row.get("direction") or None,
                    _decimal_or_none(row.get("amount_min", ""), "rules"),
                    _decimal_or_none(row.get("amount_max", ""), "rules"),
                    row["article_id"],
                    _bool(row.get("active", "true")) if row.get("active") else True,
                    row.get("comment") or None,
                ),
            )
        report.rules = len(rules)

        cur.execute("DELETE FROM finance.manual_overrides")
        for row in overrides:
            cur.execute(
                """
                INSERT INTO finance.manual_overrides (operation_id, article_id, comment)
                VALUES (%s, %s, %s)
                """,
                (row["operation_id"], row["article_id"], row.get("comment") or None),
            )
        report.overrides = len(overrides)

        cur.execute("DELETE FROM finance.loan_schedule")
        for row in loan:
            cur.execute(
                """
                INSERT INTO finance.loan_schedule
                    (loan_id, period_no, due_date, payment_total, interest, principal,
                     balance_after, comment)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row.get("loan_id") or "main", int(row["period_no"]),
                    date.fromisoformat(row["due_date"]),
                    _decimal(row["payment_total"], "loan"), _decimal(row["interest"], "loan"),
                    _decimal(row["principal"], "loan"),
                    _decimal_or_none(row.get("balance_after", ""), "loan"),
                    row.get("comment") or None,
                ),
            )
        report.loan_periods = len(loan)

    conn.commit()
    log.info(
        "Справочники загружены: статей %s, правил %s, ручных разметок %s, периодов кредита %s",
        report.articles, report.rules, report.overrides, report.loan_periods,
    )
    return report
