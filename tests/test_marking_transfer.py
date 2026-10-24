"""Перенос старых привязок кодов из пула в `order_marking_codes` при старте."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core import database
from app.modules.marking.codes import GS
from app.modules.marking.models import RETURNED, SCANNED, SOLD, MarkingCodeRow, OrderMarkingCode

GTIN = "04606203099221"


def legacy(serial: str, **fields) -> MarkingCodeRow:
    values = dict(code=f"01{GTIN}21{serial}{GS}93dGVz", gtin=GTIN, serial=serial,
                  status="in_stock", from_pool=True, manual=False)
    values.update(fields)
    return MarkingCodeRow(**values)


async def seed(db) -> None:
    """Пул в том виде, в каком его оставила версия до 16.10.2026."""
    now = datetime.now(timezone.utc)
    async with db() as session:
        session.add_all([
            legacy("OLD000000001", order_id=11, item_index=0, status="assigned",
                   scanned_at=now, scanned_by="Оля"),
            legacy("OLD000000002", order_id=11, item_index=1, status="sold",
                   scanned_at=now, sold_at=now, from_pool=False),
            legacy("POOL00000001"),  # просто в пуле — переносить нечего
        ])
        await session.commit()


async def order_codes(db) -> list[OrderMarkingCode]:
    async with db() as session:
        return list((await session.execute(
            select(OrderMarkingCode).order_by(OrderMarkingCode.serial)
        )).scalars().all())


async def test_transfer_twice_in_a_row_moves_once(clean):
    await seed(clean)
    assert await database._move_marking(database._engine) == 2
    assert await database._move_marking(database._engine) == 0

    rows = await order_codes(clean)
    assert [(r.serial, r.order_id, r.item_index, r.status) for r in rows] == [
        ("OLD000000001", 11, 0, SCANNED),
        ("OLD000000002", 11, 1, SOLD),
    ]
    assert rows[0].raw_code == f"01{GTIN}21OLD000000001{GS}93dGVz"
    assert rows[0].scanned_by == "Оля" and rows[0].from_pool and not rows[1].from_pool


async def test_already_known_pair_is_skipped(clean):
    """Код, который новая таблица уже знает (даже возвращённым), второй строкой не заводится."""
    await seed(clean)
    async with clean() as session:
        session.add(OrderMarkingCode(order_id=99, item_index=0, raw_code="x", gtin=GTIN,
                                     serial="OLD000000001", status=RETURNED))
        await session.commit()
    assert await database._move_marking(database._engine) == 1
    serials = [(r.serial, r.order_id) for r in await order_codes(clean)]
    assert serials == [("OLD000000001", 99), ("OLD000000002", 11)]


async def test_failed_transfer_leaves_the_database_as_it_was(clean, monkeypatch):
    await seed(clean)
    # Сверка «не сошлась» — вставка уже сделана в той же транзакции и должна откатиться.
    monkeypatch.setattr(database, "_MARKING_TRANSFER_CHECK", "SELECT 1")
    assert await database._move_marking(database._engine) == 0
    assert await order_codes(clean) == []

    monkeypatch.undo()
    assert await database._move_marking(database._engine) == 2


async def test_startup_runs_the_transfer(clean):
    """Перенос подключён к старту: `_connect` зовёт его после схемы."""
    await seed(clean)
    assert await database._connect()
    assert len(await order_codes(clean)) == 2
