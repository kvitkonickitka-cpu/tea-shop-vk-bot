"""Ввод кодов «Честного знака» в оборот — ручной шаг с учётом в базе (пакет E).

Интеграции с ГИС МТ и подписи УКЭП на сервере нет и не будет: подпись —
у владельцев. Поэтому:

1. `/codes <GTIN> <количество>` в чате Ops — бот берёт из пула (СУЗ) свободные
   коды, которые ещё не печатались и не списаны, заводит партию
   (`fin.code_batches`, статус «не подан») и присылает файл со списком КИ
   (01 + GTIN + 21 + серийный номер) для загрузки в личный кабинет ЧЗ;
2. партию подали в ЛК — `/circulation <партия> подан`, ЛК принял —
   `/circulation <партия> принят` (или формой на дашборде «Финансы»);
3. испорченная этикетка — `/spoil <серийный номер или код> <причина>` —
   код списан (`fin.code_writeoffs`) и в заказ попадать не должен.

Контроль (B1, B3): каждый код в закрывающем чеке — из принятой партии и не
списан. Коды заказов читаются и из прежней привязки в пуле
(`marking_codes.order_id`), и из `order_marking_codes`, если её уже завела
ветка маркировки. Таблицы — в схеме `fin` (миграция 008): нет миграции —
команды отвечают понятной ошибкой, контроль — «нет данных».
TODO(chz): формат файла для ЛК ЧЗ сверить на первой подаче.
"""

from __future__ import annotations

import csv
import io
import logging

from sqlalchemy import text

from app.core.database import get_session_factory

logger = logging.getLogger(__name__)

STATUSES = ("не подан", "подан", "принят")
REASONS = {"этикетка": "испорчена этикетка", "испорчена": "испорчена этикетка", "брак": "брак печати",
           "печать": "брак печати", "утеря": "утеря", "потеря": "утеря", "другое": "другое"}


async def _rows(sql: str, **params):
    async with get_session_factory()() as session:
        return (await session.execute(text(sql), params)).all()


async def ready() -> bool:
    return bool((await _rows("select to_regclass('fin.code_batches') is not null"))[0][0])


async def _require() -> None:
    if not await ready():
        raise ValueError("Таблиц ввода в оборот нет — миграция финмодели 008 не применена.")


def ki(gtin: str, serial: str) -> str:
    """Код идентификации без криптохвоста — то, что ЛК ЧЗ ждёт при вводе в оборот."""
    return f"01{gtin}21{serial}"


async def create_batch(gtin: str, count: int, *, by: str, product: str | None = None) -> tuple[int, list[tuple]]:
    await _require()
    if count < 1 or count > 5000:
        raise ValueError("Количество — от 1 до 5000.")
    async with get_session_factory()() as session:
        codes = (await session.execute(text(
            "select m.id, m.gtin, m.serial from marking_codes m where m.gtin = :g and m.status = 'in_stock'"
            " and m.from_pool and not exists (select 1 from fin.code_batch_items i where i.code_id = m.id)"
            " and not exists (select 1 from fin.code_writeoffs w where w.gtin = m.gtin and w.serial = m.serial)"
            " order by m.id limit :n"), {"g": gtin, "n": count})).all()
        if not codes:
            raise ValueError(f"Свободных кодов GTIN {gtin} в пуле нет — загрузите выгрузку СУЗ (codes/import).")
        batch_id = (await session.execute(text(
            "insert into fin.code_batches (gtin, product, codes_count, entered_by, note)"
            " values (:g, :p, :c, 'бот', :by) returning id"), {"g": gtin, "p": product, "c": len(codes), "by": by})).scalar()
        for code_id, code_gtin, serial in codes:
            await session.execute(text(
                "insert into fin.code_batch_items (batch_id, code_id, gtin, serial) values (:b, :i, :g, :s)"),
                {"b": batch_id, "i": code_id, "g": code_gtin, "s": serial})
        await session.commit()
    logger.info("Партия кодов %s: GTIN %s, %s шт.", batch_id, gtin, len(codes))
    return batch_id, codes


def export_csv(codes: list[tuple]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(("КИ", "GTIN", "Серийный номер"))
    for _, gtin, serial in codes:
        writer.writerow((ki(gtin, serial), gtin, serial))
    return ("﻿" + out.getvalue()).encode("utf-8")


async def set_status(batch_id: int, status: str, *, by: str) -> bool:
    await _require()
    if status not in STATUSES:
        raise ValueError("Статус — «подан» или «принят».")
    column = {"подан": "submitted_at", "принят": "accepted_at"}.get(status)
    extra = f", {column} = coalesce({column}, current_date)" if column else ""
    async with get_session_factory()() as session:
        done = await session.execute(text(
            f"update fin.code_batches set status = :s{extra}, note = coalesce(note, '') || :n where id = :i"),
            {"s": status, "n": f" · {status}: {by}", "i": batch_id})
        await session.commit()
    return bool(done.rowcount)


async def write_off(raw: str, reason: str, *, by: str) -> str:
    """Списать код по серийному номеру или коду целиком. Возвращает GTIN + серийник."""
    await _require()
    from app.modules.marking import codes as marking_codes

    reason_text = REASONS.get((reason or "").casefold())
    if reason_text is None:
        raise ValueError("Причина — этикетка, брак, утеря или другое.")
    serial = raw.strip()
    gtin = None
    try:
        parsed = marking_codes.parse(raw)
        gtin, serial = parsed.gtin, parsed.serial
    except Exception:
        pass
    rows = await _rows("select id, gtin, serial, status from marking_codes where serial = :s"
                       + (" and gtin = :g" if gtin else ""), s=serial, g=gtin)
    if not rows:
        raise ValueError("Такого кода в пуле нет.")
    if len(rows) > 1:
        raise ValueError("Серийный номер есть у нескольких GTIN — пришлите код целиком.")
    code_id, gtin, serial, status = rows[0]
    if status in ("assigned", "sold"):
        raise ValueError("Код уже в заказе — списывать нельзя, разберите заказ.")
    async with get_session_factory()() as session:
        await session.execute(text(
            "insert into fin.code_writeoffs (code_id, gtin, serial, reason, note, entered_by)"
            " values (:i, :g, :s, :r, :n, 'бот') on conflict (gtin, serial) do nothing"),
            {"i": code_id, "g": gtin, "s": serial, "r": reason_text, "n": by})
        await session.commit()
    return f"{gtin} / {serial}"


async def list_batches(limit: int = 15) -> list:
    await _require()
    return await _rows("select id, gtin, codes_count, status, used_in_orders from fin.v_code_batches limit :n", n=limit)


_ORDER_CODES = (
    "select order_id, gtin, serial from marking_codes where order_id is not null"
)


async def _order_codes_sql() -> str:
    """Коды заказов: прежняя привязка в пуле и новая таблица ветки маркировки, если она есть."""
    if (await _rows("select to_regclass('public.order_marking_codes') is not null"))[0][0]:
        return (_ORDER_CODES + " union select order_id, gtin, serial from order_marking_codes"
                " where status <> 'returned'")
    return _ORDER_CODES


async def not_in_circulation(*, since=None, until=None) -> list[int] | None:
    """Заказы с закрывающим чеком, где код не из принятой партии или списан. None — учёта ещё нет."""
    if not await ready():
        return None
    window = ""
    if since is not None:
        window = " and o.settlement_receipt_sent_at >= :since and o.settlement_receipt_sent_at < :until"
    rows = await _rows(
        f"with c as ({await _order_codes_sql()})"
        " select distinct c.order_id from c join orders o on o.id = c.order_id"
        " where not o.is_test and o.settlement_receipt_status = 'succeeded'" + window +
        " and (not exists (select 1 from fin.code_batch_items i join fin.code_batches b on b.id = i.batch_id"
        "                  where i.gtin = c.gtin and i.serial = c.serial and b.status = 'принят')"
        "      or exists (select 1 from fin.code_writeoffs w where w.gtin = c.gtin and w.serial = c.serial))"
        " order by 1", since=since, until=until)
    return [r[0] for r in rows]


async def sold_vs_codes(since, until) -> tuple[int, int] | None:
    """За период: пачек и сетов в заказах с закрывающим чеком против выбывших кодов этих заказов."""
    if not await ready():
        return None
    units = (await _rows(
        "select coalesce(sum((it->>'quantity')::int), 0) from orders o, jsonb_array_elements(o.items) it"
        " where not o.is_test and o.settlement_receipt_status = 'succeeded'"
        " and o.settlement_receipt_sent_at >= :s and o.settlement_receipt_sent_at < :u", s=since, u=until))[0][0]
    codes = (await _rows(
        f"with c as ({await _order_codes_sql()})"
        " select count(*) from c join orders o on o.id = c.order_id where not o.is_test"
        " and o.settlement_receipt_status = 'succeeded'"
        " and o.settlement_receipt_sent_at >= :s and o.settlement_receipt_sent_at < :u", s=since, u=until))[0][0]
    return int(units or 0), int(codes or 0)
