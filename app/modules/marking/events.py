"""Журнал статусов кода: запись событий из кода бота.

Одно правило на все места: событие пишется в той же транзакции, что и
изменение, ради которого оно случилось (импорт пула, «Собрано», чек, чек
возврата), — иначе журнал и привязка к заказу могли бы разойтись.
"""

from __future__ import annotations

from sqlalchemy import text

from app.modules.marking.schema import STATUSES


async def record(session, codes: list[tuple[str, str]], status: str, *, by: str = "бот",
                 order_id: int | None = None, note: str | None = None) -> int:
    """Записать событие `status` каждому коду (GTIN, серийный номер). Возвращает, сколько записано."""
    if status not in STATUSES:
        raise ValueError(f"нет такого статуса кода: {status}")
    if not codes:
        return 0
    await session.execute(text("""
        insert into shop.code_events (gtin, serial, status, by, order_id, note)
        select gtin, serial, :status, :by, :order_id, :note
        from unnest(cast(:gtins as text[]), cast(:serials as text[])) as c (gtin, serial)"""),
        {"status": status, "by": by or "бот", "order_id": order_id, "note": note,
         "gtins": [g for g, _ in codes], "serials": [s for _, s in codes]})
    return len(codes)


async def current(session, gtin: str, serial: str) -> str | None:
    """Текущий статус кода — последнее событие журнала, или None, если событий нет."""
    return await session.scalar(text(
        "select status from shop.code_events where gtin = :g and serial = :s order by id desc limit 1"),
        {"g": gtin, "s": serial})
