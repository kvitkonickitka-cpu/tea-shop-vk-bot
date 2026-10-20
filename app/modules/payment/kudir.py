"""КУДиР (книга учёта доходов, УСН «доходы»): выгрузка за период в xlsx и csv.

Источник — реестр ЮKassa (`finance.yk_payments`, `finance.yk_refunds`) и
выписка банка: прочие поступления статей группы «Выручка» (переводы
покупателей напрямую, Ozon). Вклады, займы и переводы на свои счета в
выручку cashflow не относит — в книгу они не попадают.

- доход по платежу — сумма, которую заплатил покупатель (`amount`, брутто):
  комиссия ЮKassa при УСН «доходы» доход не уменьшает;
- возврат — минусом в периоде возврата;
- дата дохода — настройка `KUDIR_INCOME_DATE`: `captured_at` (деньги
  списаны с покупателя, по умолчанию) или `payout_date` (ЮKassa выплатила на
  счёт). Бухгалтер 21.10.2026: оставить дату оплаты — осторожный вариант,
  доход не уезжает в следующий период и совпадает с чеком.

Витрина `finance.v_yk_kudir` пересоздаётся при старте бота с правилом из
настройки — тем же SQL пользуется выгрузка, так что файл и дашборд совпадают.
Персональных данных нет: номер заказа, id платежа, статья.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_session_factory

RULE_NOTE = ("Дата дохода — {rule}. Правило подтверждено бухгалтером 21.10.2026 (ст. 346.17 НК РФ), "
             "записать в учётную политику.")
RULES = {"captured_at": "дата оплаты покупателем", "payout_date": "дата выплаты ЮKassa на счёт"}
COLUMNS = ("№", "Дата и номер документа", "Содержание операции", "Доходы, ₽")


def rule() -> str:
    value = (settings.kudir_income_date or "captured_at").strip()
    return value if value in RULES else "captured_at"


def _view_sql(with_bank: bool) -> str:
    payout = rule() == "payout_date"
    pay_date = "p.payout_date" if payout else "(p.captured_at at time zone 'Europe/Moscow')::date"
    ref_date = "r.payout_date" if payout else "(r.created_at at time zone 'Europe/Moscow')::date"
    bank = (
        "  union all\n"
        "  select f.operation_date, f.operation_id, f.operation_date, a.article, f.amount, 'bank'\n"
        "  from finance.fact_operations f join finance.articles a using (article_id)\n"
        "  where f.direction = 'in' and a.grp = 'Выручка' and f.article_id <> 'cfo_in_yookassa'\n"
    ) if with_bank else ""
    return (
        "create or replace view finance.v_yk_kudir as\n"
        "select income_date, doc_number, doc_date, content, income::numeric(14,2) as income, kind from (\n"
        f"  select {pay_date} as income_date, p.id as doc_number,\n"
        "         (p.captured_at at time zone 'Europe/Moscow')::date as doc_date,\n"
        "         'Оплата покупателя' || coalesce(' по заказу №' || p.order_id, '') || ' через ЮKassa'\n"
        "           || coalesce(' (' || p.payment_method || ')', '') as content,\n"
        "         p.amount as income, 'payment' as kind\n"
        "  from finance.yk_payments p where p.status = 'succeeded' and p.captured_at is not null\n"
        "  union all\n"
        f"  select {ref_date}, r.id, (r.created_at at time zone 'Europe/Moscow')::date,\n"
        "         'Возврат покупателю' || coalesce(' по заказу №' || r.order_id, ''), -r.amount, 'refund'\n"
        "  from finance.yk_refunds r where r.status = 'succeeded'\n"
        f"{bank}"
        ") k where income_date is not null"
    )


def view_ddl(with_bank: bool | None = None) -> list[str]:
    """Пересоздать витрину. with_bank=None — решит ensure_view по наличию выписки."""
    return [] if with_bank is None else [
        _view_sql(with_bank),
        f"comment on view finance.v_yk_kudir is 'КУДиР: доходы по реестру ЮKassa и прочая выручка выписки. "
        f"{RULE_NOTE.format(rule=RULES[rule()])}'",
    ]


async def ensure_view(conn) -> None:
    with_bank = (await conn.exec_driver_sql(
        "select to_regclass('finance.fact_operations') is not null and to_regclass('finance.articles') is not null"
    )).scalar()
    # Тип столбца меняется только пересозданием; витрину читают fin.* — тогда пропускаем.
    for statement in view_ddl(bool(with_bank)):
        await conn.exec_driver_sql(statement)


async def rows(start: date, end: date) -> list[dict]:
    """Записи книги за [start, end] включительно, по дате дохода."""
    async with get_session_factory()() as session:
        result = await session.execute(text(
            "select income_date, doc_number, doc_date, content, income, kind from finance.v_yk_kudir"
            " where income_date between :s and :e order by income_date, doc_date, doc_number"
        ), {"s": start, "e": end})
        return [dict(r._mapping) for r in result]


def _document(row: dict) -> str:
    return f"{row['doc_date']:%d.%m.%Y} № {row['doc_number']}"


def to_csv(records: list[dict]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(("Дата дохода", *COLUMNS[1:]))
    for row in records:
        writer.writerow((f"{row['income_date']:%d.%m.%Y}", _document(row), row["content"],
                         str(row["income"]).replace(".", ",")))
    writer.writerow(("Итого", "", "", str(sum((r["income"] for r in records), Decimal(0))).replace(".", ",")))
    # BOM — чтобы Excel открыл кириллицу без вопросов.
    return ("﻿" + out.getvalue()).encode("utf-8")


def to_xlsx(records: list[dict], start: date, end: date) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = "Доходы"
    sheet.append([f"КУДиР, раздел I. Доходы за {start:%d.%m.%Y} – {end:%d.%m.%Y}"])
    sheet.append([RULE_NOTE.format(rule=RULES[rule()])])
    sheet.append([])
    sheet.append(list(COLUMNS[:1]) + ["Дата дохода"] + list(COLUMNS[1:]))
    for cell in sheet[4]:
        cell.font = Font(bold=True)
    for number, row in enumerate(records, start=1):
        sheet.append([number, row["income_date"], _document(row), row["content"], float(row["income"])])
        sheet.cell(row=sheet.max_row, column=2).number_format = "DD.MM.YYYY"
    total = sum((r["income"] for r in records), Decimal(0))
    sheet.append(["", "", "", "Итого за период", float(total)])
    sheet.cell(row=sheet.max_row, column=4).font = Font(bold=True)
    for column, width in zip("ABCDE", (6, 13, 38, 60, 14)):
        sheet.column_dimensions[column].width = width
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def parse_period(args: list[str], today: date) -> tuple[date, date]:
    """`2026-10` — месяц; `2026-10-01 2026-12-31` — интервал; `2026` — год; пусто — прошлый месяц."""
    def month_bounds(year: int, month: int) -> tuple[date, date]:
        first = date(year, month, 1)
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        return first, nxt.fromordinal(nxt.toordinal() - 1)

    if not args:
        first = today.replace(day=1)
        last_month = first.fromordinal(first.toordinal() - 1)
        return month_bounds(last_month.year, last_month.month)
    if len(args) == 2:
        start, end = date.fromisoformat(args[0]), date.fromisoformat(args[1])
        if end < start:
            raise ValueError("Конец периода раньше начала.")
        return start, end
    raw = args[0]
    if len(raw) == 4 and raw.isdigit():
        return date(int(raw), 1, 1), date(int(raw), 12, 31)
    if len(raw) == 7:
        year, month = raw.split("-")
        return month_bounds(int(year), int(month))
    raise ValueError("Период: 2026-10, 2026 или две даты 2026-10-01 2026-12-31.")
