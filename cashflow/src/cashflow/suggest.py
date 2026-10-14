"""Черновик правил по операциям, которые не разобрались.

Персональные данные не выгружаются: предложения строятся только по юрлицам
(ИНН 10 знаков) и по типовым фразам назначения платежа. Физлица считаются
одной обезличенной строкой.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

STOP_WORDS = {"оплата", "перевод", "по", "за", "от", "на", "счет", "счёт", "договору", "ндс"}


@dataclass
class Suggestion:
    field: str
    match_type: str
    value: str
    direction: str
    operations: int
    total_amount: Decimal
    example_purpose: str


def _phrase(purpose: str | None) -> str:
    """Типовая фраза назначения: первые значимые слова без цифр и реквизитов."""
    if not purpose:
        return ""
    words = re.findall(r"[А-Яа-яЁёA-Za-z]{3,}", purpose)
    meaningful = [w for w in words if w.lower() not in STOP_WORDS]
    return " ".join(meaningful[:3]).lower()


def collect(conn: psycopg.Connection) -> list[Suggestion]:
    suggestions: list[Suggestion] = []

    with conn.cursor() as cur:
        # Юрлица: самый надёжный признак — ИНН.
        cur.execute(
            """
            SELECT counterparty_inn, min(counterparty_name), direction,
                   count(*), sum(amount), min(purpose)
            FROM finance.fact_operations
            WHERE article_id = 'tech_unclassified' AND counterparty_kind = 'org'
            GROUP BY counterparty_inn, direction
            ORDER BY sum(amount) DESC
            """
        )
        for inn, name, direction, count, total, purpose in cur.fetchall():
            suggestions.append(
                Suggestion("counterparty_inn", "equals", inn, direction, count, total,
                           f"{name or ''} — {(purpose or '')[:60]}")
            )

        # Фразы в назначении: работают и для физлиц, но имён не раскрывают.
        cur.execute(
            """
            SELECT purpose, direction, amount
            FROM finance.fact_operations
            WHERE article_id = 'tech_unclassified'
            """
        )
        buckets: dict[tuple[str, str], list[Decimal]] = {}
        for purpose, direction, amount in cur.fetchall():
            phrase = _phrase(purpose)
            if not phrase:
                continue
            buckets.setdefault((phrase, direction), []).append(amount)

    for (phrase, direction), amounts in buckets.items():
        if len(amounts) < 2:
            continue
        suggestions.append(
            Suggestion("purpose", "contains", phrase, direction, len(amounts),
                       sum(amounts, Decimal("0")), "")
        )

    suggestions.sort(key=lambda s: (s.total_amount, s.operations), reverse=True)
    return suggestions


def write_csv(suggestions: list[Suggestion], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["priority", "field", "match_type", "value", "direction", "amount_min",
             "amount_max", "article_id", "active", "comment"]
        )
        for i, s in enumerate(suggestions, start=1):
            writer.writerow(
                [500 + i, s.field, s.match_type, s.value, s.direction, "", "",
                 "ВПИШИТЕ_СТАТЬЮ", "false",
                 f"операций {s.operations}, сумма {s.total_amount} ₽ {s.example_purpose}".strip()]
            )
    return path


def run_suggest(conn: psycopg.Connection, data_dir: Path) -> tuple[int, Path]:
    suggestions = collect(conn)
    path = write_csv(suggestions, data_dir / "suggested_rules.csv")
    log.info("Предложено правил: %s → %s", len(suggestions), path)
    return len(suggestions), path
