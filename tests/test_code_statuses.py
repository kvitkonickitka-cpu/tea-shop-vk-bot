"""Коды «Честного знака» по статусам: журнал, ввод в оборот, запрет отгрузки, коды партии.

Каталог, партии и резервы — в схеме shop (`tests.test_stock.articles`), бот
читает каталог из базы. Код проходит весь путь на одном заказе: получен из
СУЗ → наклеен при сборке (связан со строкой заказа и партией) → введён в
оборот выгрузкой → продан закрывающим чеком → возвращён чеком возврата.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.modules.catalog import store
from app.modules.marking import circulation, packing, pool
from app.modules.marking.codes import GS
from app.modules.ops import commands
from app.modules.payment import settlement
from app.modules.stock import service as stock
from tests.test_catalog_db import GTIN_A, lot, shop  # noqa: F401
from tests.test_stock import TGI, articles, order  # noqa: F401

GTIN = "0" + GTIN_A


def code(serial: str, gtin: str = GTIN) -> str:
    return f"01{gtin}21{serial}{GS}93dGVz"


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "token-for-tests")
    monkeypatch.setattr(settings, "ops_admin_tg_ids", "")
    monkeypatch.setattr(settings, "ops_assistant_allowed_tg_ids", "")


async def history(conn, serial):
    return [r["status"] for r in await conn.fetch(
        "select status from shop.code_events where serial = $1 order by id", serial)]


async def status(conn, serial):
    return await conn.fetchval("select status from shop.v_code_status where serial = $1", serial)


async def packed_order(clean, conn, serials, quantity=1):
    """Оплаченный заказ пачек ТГИ: резерв из партии, коды отсканированы, «Собрано»."""
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": quantity, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    await stock.reserve_order(paid.id)
    for serial in serials:
        assert (await packing.scan(paid.id, code(serial), by="Оля")).ok
    await packing.finish(paid.id, by="Оля")
    return paid


async def test_full_path_on_one_order(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await store.ensure_fresh(force=True)
    await pool.import_codes("\n".join(code(f"TGI00000000{i}") for i in range(1, 4)))
    assert await history(articles, "TGI000000001") == ["received"]

    paid = await packed_order(clean, articles, ["TGI000000001"])
    assert await status(articles, "TGI000000001") == "applied"
    # Код ↔ строка заказа ↔ партия.
    link = await articles.fetchrow("select * from shop.v_code_lots where serial = 'TGI000000001'")
    assert (link["order_id"], link["item_index"], link["lot_code"]) == (paid.id, 0, "L-261101-TGI")

    batch_id, rows = await circulation.create_batch(by="tg:1")
    assert [(r[1], r[3]) for r in rows] == [("TGI000000001", paid.id)]
    assert await circulation.mark_introduced(batch_id, by="tg:1", document="ПР-1") == 1
    assert await status(articles, "TGI000000001") == "introduced"
    assert await circulation.order_introduced(paid.id)

    await packing.mark_sold(paid.id)
    await packing.mark_returned(paid.id, "ref-1")
    assert await history(articles, "TGI000000001") == ["received", "applied", "introduced", "sold", "returned"]
    # Свободные коды пула остались «получены».
    assert await status(articles, "TGI000000002") == "received"


async def test_spoiled_label_gives_a_new_code_and_the_old_one_is_spoiled(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await store.ensure_fresh(force=True)
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 1, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    first = await packing.scan(paid.id, code("TGISPOILED01"), by="Оля")
    code_id = first.state.positions[0].codes[0]["id"]
    await packing.spoil(paid.id, code_id, by="Оля")
    assert await status(articles, "TGISPOILED01") == "spoiled"
    again = await packing.scan(paid.id, code("TGISPOILED01"), by="Оля")
    assert not again.ok and "испорчен" in again.message
    assert (await packing.scan(paid.id, code("TGINEWCODE01"), by="Оля")).ok
    await packing.finish(paid.id, by="Оля")
    assert await status(articles, "TGINEWCODE01") == "applied"


async def test_no_shipping_and_no_receipt_before_introduction(clean, articles, monkeypatch):
    monkeypatch.setattr(settings, "require_introduced_before_ship", True)
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await store.ensure_fresh(force=True)
    paid = await packed_order(clean, articles, ["TGISHIP00001"])
    async with clean() as session:
        await session.execute(text(
            "update orders set details = details || '{\"recipient_email\": \"a@b.ru\"}', payment_id = 'pay-s1' "
            "where id = :o"), {"o": paid.id})
        await session.commit()
    shipped = []

    async def record(order_id, event, *, source, at=None):
        shipped.append((order_id, event, source))

    from app.modules.orders import delivery_events

    monkeypatch.setattr(delivery_events, "record", record)
    with pytest.raises(packing.LinkError, match="не введены в оборот"):
        await packing.ship(paid.id)
    from app.modules.orders import repository

    with pytest.raises(settlement.NotReady, match="не введены в оборот"):
        await settlement._check_ready(await repository.by_id(paid.id))
    assert not (await packing.state(paid.id)).introduced

    batch_id, _ = await circulation.create_batch(by="tg:1")
    await circulation.mark_introduced(batch_id, by="tg:1")
    await packing.ship(paid.id, by="Оля")
    assert shipped == [(paid.id, delivery_events.HANDED_OVER, "сборка")]
    assert await settlement._check_ready(await repository.by_id(paid.id))


async def test_batch_takes_only_applied_codes_once(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await store.ensure_fresh(force=True)
    await pool.import_codes("\n".join([code("TGIPOOL00001"), code("TGIBATCH0001")]))
    with pytest.raises(Exception, match="Нечего выгружать"):
        await circulation.create_batch(by="tg:1")
    await packed_order(clean, articles, ["TGIBATCH0001"])
    first, rows = await circulation.create_batch(by="tg:1")
    assert [r[1] for r in rows] == ["TGIBATCH0001"]
    with pytest.raises(Exception, match="Нечего выгружать"):
        await circulation.create_batch(by="tg:1")
    # Отменённая выгрузка отдаёт коды следующей.
    assert await circulation.cancel_batch(first)
    second, rows = await circulation.create_batch(by="tg:1")
    assert [r[1] for r in rows] == ["TGIBATCH0001"] and second != first
    text_ = circulation.export_csv(rows, "ЕАЭС N RU Д-CN.РА01.В.12345/26", "01.11.2026").decode("utf-8-sig")
    assert text_.splitlines()[0] == "КИ;GTIN;Вид документа;Номер декларации;Дата декларации;Дата упаковки"
    assert f"01{GTIN}21TGIBATCH0001;{GTIN};Декларация о соответствии;ЕАЭС N RU Д-CN.РА01.В.12345/26;01.11.2026;" in text_


async def test_ops_commands_spoil_withdraw_and_introduce(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await store.ensure_fresh(force=True)
    await pool.import_codes("\n".join([code("TGIOPS000001"), code("TGIOPS000002")]))
    sender = {"id": 1, "username": "nikita"}
    reply = await commands.spoil_command(["TGIOPS000001", "этикетка"], sender)
    assert "испорчен" in reply and await status(articles, "TGIOPS000001") == "spoiled"
    with pytest.raises(ValueError, match="только введённый"):
        await circulation.withdraw("TGIOPS000002", "брак", by="tg:1")
    await packed_order(clean, articles, ["TGIOPS000002"])
    with pytest.raises(ValueError, match="нет номера и даты|Нет номера"):
        await commands.codes_command(["новая"], sender)

    async def declaration():
        return "ЕАЭС N RU Д-CN.РА01.В.12345/26", "01.11.2026"

    import app.modules.marking.circulation as module

    original = module.declaration
    module.declaration = declaration
    try:
        reply = await commands.codes_command(["новая"], sender)
    finally:
        module.declaration = original
    batch_id = int(reply.split("<code>")[1].split("</code>")[0])
    reply = await commands.circulation_command([str(batch_id), "введено", "ПР-7"], sender)
    assert "введено в оборот 1" in reply
    reply = await commands.withdraw_command(["TGIOPS000002", "просрочка"], sender)
    assert "выведен" in reply and await status(articles, "TGIOPS000002") == "withdrawn"


async def test_metabase_forms_make_and_close_a_batch(clean, articles):
    import sys
    import types
    from pathlib import Path

    from tests.test_catalog_forms import render, run_as_form

    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await store.ensure_fresh(force=True)
    await packed_order(clean, articles, ["TGIFORM00001"])
    created = []

    def call(method, path, body=None):
        if path == "/dataset":
            return {"data": {"rows": [["1 · 24.10 18:00 · 1 кодов"]]}}
        if path == "/action":
            created.append(body)
            return {"id": len(created)}
        if path == "/card":
            return {"id": 900}
        raise AssertionError(path)

    fake = types.ModuleType("mbapi")
    fake.BASE, fake.call = "https://stats.example/api", call
    sys.modules["mbapi"] = fake
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analytics" / "metabase"))
    for name in ("fin_build", "dash_build"):
        sys.modules.pop(name, None)
    try:
        import dash_build  # noqa: PLC0415

        dash_build.code_forms(1, 9, {"shop.code_batches": 31}, {})
    finally:
        sys.path.pop(0)
        for name in ("fin_build", "dash_build", "mbapi"):
            sys.modules.pop(name, None)
    sql = {a["name"]: a["dataset_query"]["native"]["query"] for a in created}
    await run_as_form(articles, render(sql["Сформировать выгрузку"], {"entered_by": "Никита"}))
    batch = await articles.fetchval("select id from shop.code_batches")
    await run_as_form(articles, render(sql["Введено"], {"batch": f"{batch} · x", "document": "ПР-9",
                                                         "entered_by": "Никита"}))
    assert await status(articles, "TGIFORM00001") == "introduced"
    # В журнал роль форм напрямую не пишет.
    import asyncpg

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await run_as_form(articles, "insert into shop.code_events (gtin, serial, status) values ('1', '2', 'sold')")


async def test_control_flags_receipt_with_code_not_introduced(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await store.ensure_fresh(force=True)
    paid = await packed_order(clean, articles, ["TGICTRL00001"])
    async with clean() as session:
        await session.execute(text("update orders set settlement_receipt_status = 'succeeded', "
                                   "settlement_receipt_sent_at = now() where id = :o"), {"o": paid.id})
        await session.commit()
    assert await circulation.not_in_circulation() == [paid.id]
    batch_id, _ = await circulation.create_batch(by="tg:1")
    await circulation.mark_introduced(batch_id, by="tg:1")
    assert await circulation.not_in_circulation() == []


# --- выгрузка «коды партии» (финмодель) ----------------------------------------------------------

from tests.test_fin_model import fin  # noqa: E402,F401


async def test_lot_export_has_every_code_of_the_lot_and_nothing_else(fin):
    await fin.execute("""
        insert into shop.lots (lot_code, raw_material, qty, unit, price_per_unit, entered_by) values
            ('L-261101-AA', 'Те Гуань Инь ЭКО (XFI0044)', 1000, 'г', 600, 'Никита'),
            ('L-261101-BB', 'Те Гуань Инь ЭКО (XFI0044)', 1000, 'г', 600, 'Никита');
        insert into shop.reservations (order_id, item_index, unit_no, sku_id, raw_material, lot_id, qty, status) values
            (501, 0, 0, 'P-T1', 'Те Гуань Инь ЭКО (XFI0044)', 1, 100, 'списан'),
            (501, 0, 1, 'P-T1', 'Те Гуань Инь ЭКО (XFI0044)', 2, 100, 'списан'),
            (502, 0, 0, 'P-T1', 'Те Гуань Инь ЭКО (XFI0044)', 1, 100, 'списан');
        insert into order_marking_codes (order_id, item_index, raw_code, gtin, serial, kind, status, from_pool, manual)
        values (501, 0, 'x', '04600000000017', 'A1', 'pack', 'sold', true, false),
               (501, 0, 'x', '04600000000017', 'B1', 'pack', 'scanned', true, false),
               (502, 0, 'x', '04600000000017', 'A2', 'pack', 'scanned', true, false);
        insert into shop.code_events (gtin, serial, status, order_id) values
            ('04600000000017', 'A1', 'applied', 501), ('04600000000017', 'A1', 'introduced', 501),
            ('04600000000017', 'A1', 'sold', 501), ('04600000000017', 'B1', 'applied', 501),
            ('04600000000017', 'A2', 'applied', 502), ('04600000000017', 'Z9', 'received', null);
    """)
    lot_a = await fin.fetch("select serial, ki from fin.v_lot_codes where lot_code = 'L-261101-AA' order by serial")
    assert [r["serial"] for r in lot_a] == ["A1", "A2"]
    assert lot_a[0]["ki"] == "010460000000001721A1"
    assert [r["serial"] for r in await fin.fetch("select serial from fin.v_lot_codes where lot_code = 'L-261101-BB'")] == ["B1"]
    by_lot = {r["lot_code"]: r for r in await fin.fetch("select * from fin.v_codes_by_lot")}
    a = by_lot["L-261101-AA"]
    assert (a["packed"], a["introduced"], a["sold"], a["at_customers"]) == (2, 1, 1, 1)
    # Наклеены и не введены, а продажа уже была — «нельзя продавать» после рабочего дня.
    await fin.execute("update shop.code_events set at = now() - interval '5 days' where status = 'applied'")
    unsellable = {r["serial"] for r in await fin.fetch("select serial from fin.v_codes_unsellable")}
    assert unsellable == {"A2", "B1"}
    attention = [r["text"] for r in await fin.fetch("select * from fin.v_attention")]
    assert any(t.startswith("Нельзя продавать: 2 код") for t in attention)
