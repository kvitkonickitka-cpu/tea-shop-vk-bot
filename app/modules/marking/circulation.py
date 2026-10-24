"""Ввод кодов «Честного знака» в оборот при фасовке под заказ.

Пачка собирается уже после оплаты, а продавать до ввода в оборот нельзя
(ст. 15.12 КоАП). Поэтому дневной цикл:

1. собрали заказы дня — коды «наклеены» (журнал `shop.code_events`);
2. выгрузка для ввода в оборот — `/codes` в Ops или кнопка «Сформировать
   выгрузку» на «Пульс → Коды»: все наклеенные и ещё не выгруженные коды с
   нашей декларацией на каждый код (`shop.code_batches`, триггер сам кладёт в
   выгрузку коды);
3. владелец загружает файл в ЛК ЧЗ, подписывает документ «Производство РФ»
   и отмечает «Введено» (`/circulation <выгрузка> введено` или кнопкой) —
   триггер пишет каждому коду «введён в оборот»;
4. только тогда — отгрузка со страницы сборки, подтверждение Ozon и
   закрывающий чек (`REQUIRE_INTRODUCED_BEFORE_SHIP`).

Интеграции с ГИС МТ (True API) и подписи на сервере нет: подпись — у
владельцев. Формат файла — простой CSV (КИ, GTIN, декларация, дата упаковки);
TODO(chz): сверить с шаблоном импорта ЛК на первой подаче.
"""

from __future__ import annotations

import csv
import io
import logging

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.marking import events

logger = logging.getLogger(__name__)

# Статусы, при которых пачку можно отгружать и продавать.
IN_CIRCULATION = ("introduced", "sold", "returned")
SPOIL_REASONS = {"этикетка": "испорчена этикетка", "испорчена": "испорчена этикетка", "брак": "брак печати",
                 "печать": "брак печати", "утеря": "утеря", "потеря": "утеря", "другое": "другое"}
WITHDRAW_REASONS = {"брак": "брак", "просрочка": "истёк срок годности", "срок": "истёк срок годности",
                    "отзыв": "отзыв партии", "другое": "другое"}


async def _rows(sql: str, **params):
    async with get_session_factory()() as session:
        return (await session.execute(text(sql), params)).all()


def ki(gtin: str, serial: str) -> str:
    """Код идентификации без криптохвоста — то, что ЛК ЧЗ ждёт при вводе в оборот."""
    return f"01{gtin}21{serial}"


async def declaration() -> tuple[str, str]:
    """Наша декларация о соответствии (ИП — изготовитель): номер и дата из вводных финмодели."""
    # Схемы fin может не быть (другая база, тесты): тогда декларации просто нет.
    if not (await _rows("select to_regclass('fin.inputs_current') is not null"))[0][0]:
        return "", ""
    rows = await _rows("""
        select (select value_text from fin.inputs_current where key = 'chz_declaration_number'),
               (select value_text from fin.inputs_current where key = 'chz_declaration_date')""")
    number, day = rows[0] if rows else (None, None)
    return (number or "").strip(), (day or "").strip()


async def create_batch(*, by: str) -> tuple[int, list[tuple]]:
    """Выгрузка для ввода в оборот: все наклеенные и ещё не выгруженные коды."""
    async with get_session_factory()() as session:
        batch_id = await session.scalar(text(
            "insert into shop.code_batches (created_by) values (:b) returning id"), {"b": by})
        await session.commit()
    return batch_id, await batch_codes(batch_id)


async def batch_codes(batch_id: int) -> list[tuple]:
    """Коды выгрузки с датой упаковки: (GTIN, серийный номер, дата упаковки, заказ)."""
    return await _rows("""
        select i.gtin, i.serial, (o.packed_at at time zone 'Europe/Moscow')::date, s.order_id
        from shop.code_batch_items i
        left join shop.v_code_status s on s.gtin = i.gtin and s.serial = i.serial
        left join orders o on o.id = s.order_id
        where i.batch_id = :b order by s.order_id, i.gtin, i.serial""", b=batch_id)


def export_csv(codes: list[tuple], number: str, day: str) -> bytes:
    """Файл для документа «Ввод в оборот → Производство РФ»: код и разрешительная документация на каждый."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(("КИ", "GTIN", "Вид документа", "Номер декларации", "Дата декларации", "Дата упаковки"))
    for gtin, serial, packed_on, _ in codes:
        writer.writerow((ki(gtin, serial), gtin, "Декларация о соответствии", number, day,
                         packed_on.strftime("%d.%m.%Y") if packed_on else ""))
    return ("﻿" + out.getvalue()).encode("utf-8")


async def mark_introduced(batch_id: int, *, by: str, document: str = "") -> int:
    """«Введено»: документ принят в ЛК ЧЗ. Возвращает, сколько кодов введено."""
    async with get_session_factory()() as session:
        status = await session.scalar(text("select status from shop.code_batches where id = :b"), {"b": batch_id})
        if status is None:
            raise ValueError(f"Выгрузки {batch_id} нет.")
        if status != "выгружена":
            raise ValueError(f"Выгрузка {batch_id} уже {status}.")
        before = await session.scalar(text("select count(*) from shop.code_events where batch_id = :b"), {"b": batch_id})
        await session.execute(text("""
            update shop.code_batches set status = 'введена', introduced_by = :by, document = nullif(:d, '')
            where id = :b"""), {"b": batch_id, "by": by, "d": document})
        after = await session.scalar(text("select count(*) from shop.code_events where batch_id = :b"), {"b": batch_id})
        await session.commit()
    return int(after - before)


async def cancel_batch(batch_id: int) -> bool:
    """Выгрузку не подали — коды снова ждут следующей."""
    async with get_session_factory()() as session:
        done = await session.execute(text(
            "update shop.code_batches set status = 'отменена' where id = :b and status = 'выгружена'"), {"b": batch_id})
        await session.commit()
    return bool(done.rowcount)


async def list_batches(limit: int = 15) -> list:
    return await _rows("""
        select b.id, b.status, count(i.serial), to_char(b.created_at at time zone 'Europe/Moscow', 'DD.MM HH24:MI')
        from shop.code_batches b left join shop.code_batch_items i on i.batch_id = b.id
        group by b.id order by b.id desc limit :n""", n=limit)


def _parse(raw: str) -> tuple[str | None, str]:
    from app.modules.marking import codes as marking_codes

    try:
        parsed = marking_codes.parse(raw)
        return parsed.gtin, parsed.serial
    except Exception:
        return None, raw.strip()


async def _find(raw: str) -> tuple[str, str, str | None]:
    gtin, serial = _parse(raw)
    rows = await _rows("""
        select distinct gtin, serial from shop.code_events where serial = :s and (cast(:g as text) is null or gtin = :g)
        union select gtin, serial from marking_codes where serial = :s and (cast(:g as text) is null or gtin = :g)""",
        s=serial, g=gtin)
    if not rows:
        raise ValueError("Такого кода нет ни в пуле, ни в журнале.")
    if len(rows) > 1:
        raise ValueError("Серийный номер есть у нескольких GTIN — пришлите код целиком.")
    gtin, serial = rows[0]
    async with get_session_factory()() as session:
        return gtin, serial, await events.current(session, gtin, serial)


async def write_off(raw: str, reason: str, *, by: str) -> str:
    """Испорченная этикетка до ввода в оборот — код «испорчен», в продажу не пойдёт."""
    reason_text = SPOIL_REASONS.get((reason or "").casefold())
    if reason_text is None:
        raise ValueError("Причина — этикетка, брак, утеря или другое.")
    gtin, serial, status = await _find(raw)
    if status in IN_CIRCULATION:
        raise ValueError("Код уже введён в оборот — выводят его из оборота: /withdraw <код> брак|просрочка|отзыв.")
    if status in ("spoiled", "withdrawn"):
        raise ValueError("Код уже списан.")
    linked = await _rows("select order_id from order_marking_codes where gtin = :g and serial = :s "
                         "and status <> 'returned'", g=gtin, s=serial)
    if linked:
        raise ValueError(f"Код в заказе №{linked[0][0]} — уберите его на странице сборки кнопкой «Этикетка испорчена».")
    async with get_session_factory()() as session:
        await events.record(session, [(gtin, serial)], "spoiled", by=by, note=reason_text)
        await session.commit()
    return f"{gtin} / {serial}"


async def withdraw(raw: str, reason: str, *, by: str) -> str:
    """Вывод из оборота после ввода: брак, просрочка, отзыв партии. Сам документ — в ЛК ЧЗ."""
    reason_text = WITHDRAW_REASONS.get((reason or "").casefold())
    if reason_text is None:
        raise ValueError("Причина — брак, просрочка, отзыв или другое.")
    gtin, serial, status = await _find(raw)
    if status not in ("introduced", "returned"):
        raise ValueError(f"Код в статусе «{status or 'нет событий'}» — выводить из оборота можно только введённый "
                         "и не проданный.")
    async with get_session_factory()() as session:
        await events.record(session, [(gtin, serial)], "withdrawn", by=by, note=reason_text)
        await session.commit()
    return f"{gtin} / {serial}"


async def order_introduced(order_id: int) -> bool:
    """Все коды заказа введены в оборот (или дальше). Нет кодов — нет и ввода."""
    rows = await _rows("""
        select s.status from order_marking_codes o
        left join shop.v_code_status s on s.gtin = o.gtin and s.serial = o.serial
        where o.order_id = :o and o.status <> 'returned'""", o=order_id)
    return bool(rows) and all(r[0] in IN_CIRCULATION for r in rows)


def must_wait() -> bool:
    return settings.require_introduced_before_ship


async def approve_introduced() -> dict:
    """Тик: собранные заказы Ozon, чьи коды уже введены, — подтвердить к отгрузке."""
    from app.modules.delivery import ozon_approve

    rows = await _rows("""
        select o.id from orders o
        where o.packed_at is not null and o.ozon_posting is not null and o.handed_over_at is null
          and coalesce(o.details ->> 'ozon_approved_at', '') = ''
          and o.packed_at > now() - interval '14 days'""")
    done = 0
    for (order_id,) in rows:
        if await order_introduced(order_id):
            await ozon_approve.after_packed(order_id)
            done += 1
    return {"подтверждено": done}


async def not_in_circulation(*, since=None, until=None) -> list[int] | None:
    """Заказы с закрывающим чеком, где код не введён в оборот или списан."""
    window = ""
    if since is not None:
        window = " and o.settlement_receipt_sent_at >= :since and o.settlement_receipt_sent_at < :until"
    rows = await _rows(
        "select distinct c.order_id from order_marking_codes c join orders o on o.id = c.order_id"
        " where not o.is_test and o.settlement_receipt_status = 'succeeded' and c.status <> 'returned'" + window +
        " and not exists (select 1 from shop.code_events e where e.gtin = c.gtin and e.serial = c.serial"
        "                 and e.status = 'introduced')"
        " order by 1", since=since, until=until)
    return [r[0] for r in rows]


async def sold_vs_codes(since, until) -> tuple[int, int] | None:
    """За период: пачек и сетов в заказах с закрывающим чеком против выбывших кодов этих заказов."""
    units = (await _rows(
        "select coalesce(sum((it->>'quantity')::int), 0) from orders o, jsonb_array_elements(o.items) it"
        " where not o.is_test and o.settlement_receipt_status = 'succeeded'"
        " and o.settlement_receipt_sent_at >= :s and o.settlement_receipt_sent_at < :u", s=since, u=until))[0][0]
    codes = (await _rows(
        "select count(*) from order_marking_codes c join orders o on o.id = c.order_id where not o.is_test"
        " and c.status <> 'returned' and o.settlement_receipt_status = 'succeeded'"
        " and o.settlement_receipt_sent_at >= :s and o.settlement_receipt_sent_at < :u", s=since, u=until))[0][0]
    return int(units or 0), int(codes or 0)
