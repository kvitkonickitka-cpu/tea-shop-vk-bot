"""Акты перевозчиков и сверка доставки (A13).

API перевозчиков: СДЭК отдаёт стоимость по заказу (`delivery_detail.total_sum`,
её пишет опрос статусов — `orders.details.carrier_actual_cost`), Ozon — только
«estimated» цены, счёт виден лишь в кабинете. Поэтому факт по Ozon (и уточнение
по СДЭКу) — из актов: форма Metabase «Акт перевозчика» или CSV командой
`/act` в чате Ops (файл с подписью `/act`): `трек;сумма[;вид;дата акта;номер акта]`,
вид — «доставка» (по умолчанию) или «возврат» (обратная доставка при невыкупе).

Отклонение факта от котировки больше `carrier_deviation_max` (10 %) по заказу
или за месяц — ⚠️ в Ops по одному разу (`check`, раз в день из тика).
Акты лежат в `fin.carrier_acts` (схема финмодели): без применённой миграции
006 импорт отвечает понятной ошибкой, а проверка молчит.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_session_factory

logger = logging.getLogger(__name__)

KINDS = {"доставка": "доставка", "delivery": "доставка", "возврат": "возврат", "return": "возврат"}


@dataclass
class ActRow:
    track: str
    amount: Decimal
    kind: str = "доставка"
    act_date: date | None = None
    act_number: str | None = None


def parse(content: bytes) -> tuple[list[ActRow], list[str]]:
    """CSV акта: строки и ошибки по номерам строк. Заголовок и пустые строки пропускаются."""
    raw = content.decode("utf-8-sig", errors="replace")
    delimiter = ";" if raw.count(";") >= raw.count(",") else ","
    rows, problems = [], []
    for number, cells in enumerate(csv.reader(io.StringIO(raw), delimiter=delimiter), start=1):
        cells = [c.strip() for c in cells]
        if not any(cells):
            continue
        try:
            amount = Decimal(cells[1].replace(" ", "").replace(" ", "").replace(",", ".")) if len(cells) > 1 else None
        except InvalidOperation:
            amount = None
        if amount is None:
            if number == 1:
                continue  # заголовок
            problems.append(f"строка {number}: нет суммы")
            continue
        if not cells[0] or amount < 0:
            problems.append(f"строка {number}: нет трека или сумма меньше нуля")
            continue
        kind = KINDS.get((cells[2] if len(cells) > 2 else "").casefold() or "доставка")
        if kind is None:
            problems.append(f"строка {number}: вид — «доставка» или «возврат»")
            continue
        act_date = None
        if len(cells) > 3 and cells[3]:
            try:
                act_date = date.fromisoformat(cells[3]) if "-" in cells[3] else \
                    date(*reversed([int(x) for x in cells[3].split(".")]))
            except ValueError:
                problems.append(f"строка {number}: дата акта — 2026-11-30 или 30.11.2026")
                continue
        rows.append(ActRow(cells[0], amount, kind, act_date, (cells[4] if len(cells) > 4 else "") or None))
    return rows, problems


async def store(rows: list[ActRow]) -> dict:
    """Записать строки акта. Трек ищется среди заказов: перевозчик — по нему."""
    async with get_session_factory()() as session:
        if not (await session.execute(text("select to_regclass('fin.carrier_acts') is not null"))).scalar():
            raise ValueError("таблицы fin.carrier_acts нет — миграция финмодели 006 не применена")
        saved, unknown = 0, []
        for row in rows:
            carrier = (await session.execute(text(
                "select case when details->>'cdek_number' = :t then 'cdek' when ozon_posting = :t then 'ozon' end"
                " from orders where details->>'cdek_number' = :t or ozon_posting = :t limit 1"), {"t": row.track})).scalar()
            if carrier is None:
                unknown.append(row.track)
                continue
            await session.execute(text(
                "insert into fin.carrier_acts (carrier, track, amount, kind, act_date, act_number, entered_by)"
                " values (:c, :t, :a, :k, :d, :n, 'бот')"),
                {"c": carrier, "t": row.track, "a": row.amount, "k": row.kind, "d": row.act_date, "n": row.act_number})
            saved += 1
        await session.commit()
    return {"записано": saved, "трек не найден": unknown}


async def check() -> dict:
    """Отклонение факта доставки от котировки — ⚠️ в Ops, по заказу и за месяц, по разу."""
    from app.messages import client as client_messages
    from app.messages import manager as manager_messages

    async with get_session_factory()() as session:
        if not (await session.execute(text("select to_regclass('fin.v_carrier_check') is not null"))).scalar():
            return {"skipped": "миграция финмодели 006 не применена"}
        orders = (await session.execute(text(
            "select order_id, carrier, carrier_quote, carrier_actual, deviation from fin.v_carrier_check where over_limit"
        ))).all()
        months = (await session.execute(text(
            "select month, carrier, quote, actual, deviation from fin.v_carrier_check_monthly where over_limit"
        ))).all()
    sent = 0
    for order_id, carrier, quote, actual, deviation in orders:
        if not await client_messages.claim_once(f"carrier_dev:{order_id}", "carrier_deviation", 0):
            continue
        await manager_messages.notify(
            manager_messages.CARRIER_CHECK,
            f"⚠️ <b>Заказ №{order_id}: доставка {_name(carrier)} {_pct(deviation)} к котировке</b>\n"
            f"Котировка {round(quote or 0)} ₽, факт {round(actual or 0)} ₽. Маржа заказа — по факту.",
            order_id=order_id, chat_id=settings.telegram_ops_chat_id or None,
        )
        sent += 1
    for month, carrier, quote, actual, deviation in months:
        if not await client_messages.claim_once(f"carrier_dev_month:{month}:{carrier}", "carrier_deviation", 0):
            continue
        await manager_messages.notify(
            manager_messages.CARRIER_CHECK,
            f"⚠️ <b>Доставка {_name(carrier)} за {month:%m.%Y}: факт {_pct(deviation)} к котировкам</b>\n"
            f"Котировки {round(quote or 0)} ₽, факт {round(actual or 0)} ₽. Проверьте тарифы и вес посылок.",
            chat_id=settings.telegram_ops_chat_id or None,
        )
        sent += 1
    return {"заказов с отклонением": len(orders), "месяцев": len(months), "отправлено": sent}


def _name(carrier: str) -> str:
    return {"cdek": "СДЭК", "ozon": "Ozon"}.get(carrier or "", carrier or "")


def _pct(deviation) -> str:
    value = float(deviation or 0) * 100
    return f"{'+' if value >= 0 else ''}{value:.0f} %"
