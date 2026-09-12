"""Расчёт аннуитетного графика кредита."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

KOPEKS = Decimal("0.01")


@dataclass(frozen=True)
class SchedulePeriod:
    period_no: int
    due_date: date
    payment_total: Decimal
    interest: Decimal
    principal: Decimal
    balance_after: Decimal


def _round(value: Decimal) -> Decimal:
    return value.quantize(KOPEKS, rounding=ROUND_HALF_UP)


def add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    # 31-е число в коротком месяце съезжает на последний день месяца.
    day = min(start.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def annuity_schedule(
    amount: Decimal, annual_rate: Decimal, months: int, first_payment: date
) -> list[SchedulePeriod]:
    """Аннуитет: платёж постоянный, доля процентов убывает.

    Последний платёж подгоняется так, чтобы остаток долга обнулился в копейку.
    """
    monthly_rate = annual_rate / Decimal(100) / Decimal(12)
    growth = (1 + monthly_rate) ** months
    payment = _round(amount * monthly_rate * growth / (growth - 1))

    schedule: list[SchedulePeriod] = []
    balance = amount
    for period_no in range(1, months + 1):
        interest = _round(balance * monthly_rate)
        if period_no == months:
            principal = balance
            total = _round(interest + principal)
        else:
            principal = payment - interest
            total = payment
        balance = _round(balance - principal)
        schedule.append(
            SchedulePeriod(
                period_no=period_no,
                due_date=add_months(first_payment, period_no - 1),
                payment_total=total,
                interest=interest,
                principal=_round(principal),
                balance_after=balance,
            )
        )
    return schedule


def write_csv(schedule: list[SchedulePeriod], path: Path, loan_id: str = "main") -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["loan_id", "period_no", "due_date", "payment_total", "interest", "principal",
             "balance_after", "comment"]
        )
        for period in schedule:
            writer.writerow(
                [loan_id, period.period_no, period.due_date.isoformat(), period.payment_total,
                 period.interest, period.principal, period.balance_after, ""]
            )
    return path
