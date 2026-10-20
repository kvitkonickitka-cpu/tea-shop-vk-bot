"""Пакет E: ввод кодов ЧЗ в оборот партиями, списание, контроль кодов в закрывающих чеках."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.modules.marking import circulation
from app.modules.ops import commands, controls
from tests.test_fin_model import fin  # noqa: F401

GTIN = "04600000000017"


async def pool(fin, n: int, gtin: str = GTIN):
    for i in range(n):
        await fin.execute(
            "insert into marking_codes (code, gtin, serial, status, from_pool, manual) values ($1, $2, $3, 'in_stock', true, false)",
            f"01{gtin}21SER{i:04d}\x1d93abcd", gtin, f"SER{i:04d}")


@pytest.fixture(autouse=True)
def admins(monkeypatch):
    monkeypatch.setattr(settings, "ops_admin_tg_ids", "")
    monkeypatch.setattr(settings, "ops_assistant_allowed_tg_ids", "")


async def test_batch_export_and_statuses(fin):
    await pool(fin, 5)
    await circulation.write_off("SER0000", "этикетка", by="tg:1")
    batch_id, rows = await circulation.create_batch(GTIN, 3, by="tg:1")
    # Списанный код в партию не попадает.
    assert [r[2] for r in rows] == ["SER0001", "SER0002", "SER0003"]
    text_ = circulation.export_csv(rows).decode("utf-8-sig")
    assert text_.splitlines()[0] == "КИ;GTIN;Серийный номер"
    assert f"01{GTIN}21SER0001;{GTIN};SER0001" in text_
    # Вторая партия берёт следующие свободные коды.
    second, more = await circulation.create_batch(GTIN, 5, by="tg:1")
    assert [r[2] for r in more] == ["SER0004"]
    assert await circulation.set_status(batch_id, "подан", by="tg:1")
    assert await circulation.set_status(batch_id, "принят", by="tg:1")
    row = await fin.fetchrow("select status, submitted_at, accepted_at from fin.code_batches where id = $1", batch_id)
    assert row["status"] == "принят" and row["submitted_at"] and row["accepted_at"]
    codes = await fin.fetchrow("select * from fin.v_codes where gtin = $1", GTIN)
    assert (codes["printed"], codes["in_circulation"], codes["written_off"]) == (4, 3, 1)
    with pytest.raises(ValueError):
        await circulation.set_status(batch_id, "выведен", by="tg:1")


async def test_settled_order_with_code_outside_accepted_batch_is_flagged(fin, db):
    from app.modules.orders.models import Order

    await pool(fin, 2)
    batch_id, _ = await circulation.create_batch(GTIN, 1, by="tg:1")
    await circulation.set_status(batch_id, "принят", by="tg:1")
    sent_at = datetime.now(timezone.utc) - timedelta(hours=3)
    async with db() as session:
        good = Order(peer_id=1, items=[{"name": "A", "quantity": 1, "price": 1}], items_total=1, delivery_cost=0,
                     total=1, status="confirmed", payment_status="succeeded", details={},
                     settlement_receipt_status="succeeded", settlement_receipt_sent_at=sent_at)
        bad = Order(peer_id=2, items=[{"name": "A", "quantity": 1, "price": 1}], items_total=1, delivery_cost=0,
                    total=1, status="confirmed", payment_status="succeeded", details={},
                    settlement_receipt_status="succeeded", settlement_receipt_sent_at=sent_at)
        session.add_all([good, bad])
        await session.commit()
        good_id, bad_id = good.id, bad.id
    await fin.execute("update marking_codes set status = 'sold', order_id = $1 where serial = 'SER0000'", good_id)
    await fin.execute("update marking_codes set status = 'sold', order_id = $1 where serial = 'SER0001'", bad_id)
    assert await circulation.not_in_circulation() == [bad_id]
    checks = {c.key: c for c in await controls.collect()}
    assert checks["codes_not_in_circulation"].orders == [bad_id]
    month = sent_at.astimezone(timezone.utc).date().replace(day=1)
    items = {i.title: i for i in await controls.month_items(month)}
    item = items["Выбывшие коды ЧЗ = проданные пачки, все — из принятых партий"]
    assert item.ok is False and "пачек и сетов 2, кодов в чеках 2, не в обороте — заказов 1" in item.detail


async def test_spoil_rules_and_commands(fin):
    await pool(fin, 2)
    await fin.execute("update marking_codes set status = 'assigned', order_id = 5 where serial = 'SER0001'")
    with pytest.raises(ValueError, match="уже в заказе"):
        await circulation.write_off("SER0001", "брак", by="tg:1")
    with pytest.raises(ValueError, match="Причина"):
        await circulation.write_off("SER0000", "просто так", by="tg:1")
    reply = await commands.run("spoil", ["SER0000", "брак"], {"id": 1})
    assert "Код списан" in reply
    assert await fin.fetchval("select reason from fin.code_writeoffs") == "брак печати"
    assert "Партий кодов ещё нет" in await commands.run("codes", [], {"id": 1})


async def test_without_migration_control_says_no_data(clean):
    assert await circulation.not_in_circulation() is None
    checks = {c.key: c for c in await controls.collect()}
    assert checks["codes_not_in_circulation"].known is False
