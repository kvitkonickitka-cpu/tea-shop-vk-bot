"""Склад по партиям: резерв при оплате, снятие, перенос, списание при сборке, срок пачки.

Каталог и партии — в той же схеме shop, что на бою (`tests.test_catalog_db.shop`),
бот читает каталог из базы (CATALOG_SOURCE=db). Заказ — строка orders.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.modules.catalog import service as catalog_service, store
from app.modules.marking import packing
from app.modules.marking.codes import GS
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order
from app.modules.stock import service as stock
from tests.test_catalog_db import GTIN_A, GTIN_B, add, lot, shop  # noqa: F401

TGI = "Те Гуань Инь ЭКО (XFI0044)"
DHP = "Да Хун Пао, сильная прожарка (XFI0005)"
BLIN = "Пуэр Танцующая Пара (IT-101)"
TODAY = date.today()


@pytest.fixture
async def articles(shop):
    """Пачка 100 г, пакетики 35 г двух сортов, набор из них и блин 357 г."""
    await add(shop, "P-TGI-ECO-100", shelf_life_months=24)
    await add(shop, "P-TGI-ECO-35", name="ТГИ 35 г", net_weight_g=35, gtin=None, price=None, on_sale=False,
              shelf_life_months=24)
    await add(shop, "P-DHP-35", name="ДХП 35 г", net_weight_g=35, raw_material=DHP, gtin=None, price=None,
              on_sale=False, shelf_life_months=24)
    await add(shop, "S-ENTRY", kind="набор", name="Набор «Точка входа»", net_weight_g=None, raw_material=None,
              gtin=GTIN_B, price=1490)
    await shop.execute("insert into shop.bundle_items (bundle_sku, item_sku, quantity) values "
                       "('S-ENTRY', 'P-TGI-ECO-35', 2), ('S-ENTRY', 'P-DHP-35', 1)")
    await add(shop, "B-DANCING-357", kind="блин", name="Блин Танцующая Пара", net_weight_g=357, raw_material=BLIN,
              gtin="4600000000039", price=1900, shelf_life_months=36)
    return shop


async def order(db, items, **fields) -> Order:
    values = dict(peer_id=8300, items=items, items_total=1000, delivery_cost=0, total=1000,
                  delivery_method="ozon_pvz", status="paid", payment_status=orders_repository.PAID,
                  payment_id="pay-s1", details={"recipient_email": "a@b.ru"}, created_at=datetime.now(timezone.utc))
    values.update(fields)
    async with db() as session:
        row = Order(**values)
        session.add(row)
        await session.commit()
        return row


async def reservations(conn, order_id):
    return await conn.fetch("select r.*, l.lot_code from shop.reservations r left join shop.lots l on l.id = r.lot_id "
                            "where order_id = $1 order by item_index, unit_no, part_no", order_id)


async def balance(conn, code):
    return await conn.fetchrow("select * from shop.v_lot_balance where lot_code = $1", code)


async def test_payment_reserves_from_the_earliest_best_before(clean, articles):
    await lot(articles, TGI, 1000, code="L-261001-LATE", best_before=TODAY + timedelta(days=700))
    await lot(articles, TGI, 1000, code="L-261002-SOON", best_before=TODAY + timedelta(days=200))
    await lot(articles, TGI, 1000, code="L-260901-OPEN")  # без срока — последней
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 2, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    result = await stock.reserve_order(paid.id)
    assert result.reserved == 2 and not result.shortages
    rows = await reservations(articles, paid.id)
    assert [r["lot_code"] for r in rows] == ["L-261002-SOON", "L-261002-SOON"]
    soon = await balance(articles, "L-261002-SOON")
    assert (soon["on_hand"], soon["reserved"], soon["free"]) == (1000, 200, 800)
    # Повторное уведомление об оплате — второго резерва нет.
    assert (await stock.reserve_order(paid.id)).reserved == 0
    assert (await balance(articles, "L-261002-SOON"))["reserved"] == 200


async def test_same_best_before_goes_by_arrival(clean, articles):
    await lot(articles, TGI, 1000, code="L-261020-NEW", arrival_date=TODAY - timedelta(days=1))
    await lot(articles, TGI, 1000, code="L-261010-OLD", arrival_date=TODAY - timedelta(days=10))
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 1, "price": 390}])  # без sku_id — по названию
    await stock.reserve_order(paid.id)
    assert [r["lot_code"] for r in await reservations(articles, paid.id)] == ["L-261010-OLD"]


async def test_set_reserves_every_component_and_blin_by_piece(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await lot(articles, DHP, 1000, code="L-261101-DHP")
    await lot(articles, BLIN, 5, unit="шт", code="L-261101-BLIN")
    paid = await order(clean, [{"name": "Набор «Точка входа»", "quantity": 1, "price": 1490, "sku_id": "S-ENTRY"},
                               {"name": "Блин Танцующая Пара", "quantity": 2, "price": 1900, "sku_id": "B-DANCING-357"}])
    await stock.reserve_order(paid.id)
    rows = await reservations(articles, paid.id)
    assert [(r["item_index"], r["sku_id"], r["qty"]) for r in rows] == [
        (0, "P-DHP-35", 35), (0, "P-TGI-ECO-35", 35), (0, "P-TGI-ECO-35", 35),
        (1, "B-DANCING-357", 1), (1, "B-DANCING-357", 1)]
    assert (await balance(articles, "L-261101-TGI"))["reserved"] == 70
    assert (await balance(articles, "L-261101-BLIN"))["free"] == 3


async def test_cancel_or_refund_before_packing_releases(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 3, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    await stock.reserve_order(paid.id)
    assert await stock.release_order(paid.id) == 3
    after = await balance(articles, "L-261101-TGI")
    assert (after["on_hand"], after["reserved"], after["free"]) == (1000, 0, 1000)
    kinds = [r["kind"] for r in await articles.fetch("select kind from shop.stock_moves order by id")]
    assert kinds == ["резерв"] * 3 + ["снятие резерва"] * 3


async def test_packer_takes_another_lot_and_reserve_moves(clean, articles):
    first = await lot(articles, TGI, 1000, code="L-261001-AA", best_before=TODAY + timedelta(days=300))
    second = await lot(articles, TGI, 1000, code="L-261002-BB", best_before=TODAY + timedelta(days=500))
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 2, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    await stock.reserve_order(paid.id)
    assert await stock.change_lot(paid.id, 0, TGI, second) == 2
    assert (await balance(articles, "L-261001-AA"))["reserved"] == 0
    assert (await balance(articles, "L-261002-BB"))["reserved"] == 200
    await stock.write_off(paid.id, by="Оля")
    b = await balance(articles, "L-261002-BB")
    assert (b["on_hand"], b["reserved"], b["written_off"]) == (800, 0, 200)
    assert first


async def test_write_off_with_fact_weight_books_the_loss(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 2, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    await stock.reserve_order(paid.id)
    await stock.write_off(paid.id, by="Оля", weights={0: 204})
    b = await balance(articles, "L-261101-TGI")
    assert (b["written_off"], b["lost"], b["on_hand"]) == (200, 4, 796)


async def test_pack_best_before_is_the_smaller_one():
    packed = date(2026, 11, 2)
    assert stock.pack_best_before(packed, 24, date(2027, 6, 1)) == date(2027, 6, 1)
    assert stock.pack_best_before(packed, 6, date(2028, 1, 1)) == date(2027, 5, 2)
    assert stock.pack_best_before(packed, None, None) is None
    assert stock.pack_best_before(date(2026, 8, 31), 6, None) == date(2027, 2, 28)


async def test_short_shelf_life_lot_is_skipped_and_packing_refused(clean, articles):
    near = await lot(articles, TGI, 1000, code="L-260101-NEAR", best_before=TODAY + timedelta(days=60))
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 1, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    result = await stock.reserve_order(paid.id)
    # Из партии с 60 днями пачку на 90 дней не собрать — резерв без партии.
    assert result.shortages and (await reservations(articles, paid.id))[0]["lot_id"] is None
    with pytest.raises(stock.StockError, match="нет партии"):
        await stock.write_off(paid.id)
    with pytest.raises(stock.StockError, match="срок"):
        await stock.change_lot(paid.id, 0, TGI, near)


async def test_shortage_alerts_ops_but_the_order_goes_on(clean, articles, monkeypatch):
    sent = []

    async def notify(kind, text_, **kwargs):
        sent.append(text_)
        return True

    from app.messages import manager as manager_messages

    monkeypatch.setattr(manager_messages, "notify", notify)
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100ops")
    await lot(articles, TGI, 150, code="L-261101-TGI")
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 2, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    result = await stock.reserve_order(paid.id)
    assert result.reserved == 2 and len(result.shortages) == 1
    assert len(sent) == 1 and "не хватает сырья" in sent[0]
    attention = await articles.fetch("select * from shop.reservations where lot_id is null and status = 'резерв'")
    assert len(attention) == 1


async def test_inventory_adjustment_and_spoilage(clean, articles):
    lot_id = await lot(articles, TGI, 1000, code="L-261101-TGI")
    on_hand = (await balance(articles, "L-261101-TGI"))["on_hand"]
    # Инвентаризация: на полке 985 г — корректировка на разницу.
    await articles.execute("insert into shop.stock_moves (lot_id, kind, qty, note) values ($1, 'корректировка', $2, "
                           "'инвентаризация')", lot_id, Decimal(985) - on_hand)
    await articles.execute("insert into shop.stock_moves (lot_id, kind, qty, note) values ($1, 'порча', 85, 'намок')",
                           lot_id)
    b = await balance(articles, "L-261101-TGI")
    assert (b["adjusted"], b["spoiled"], b["on_hand"]) == (-15, 85, 900)


async def test_not_enough_raw_material_turns_sale_off_for_the_buyer(clean, articles):
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    await store.ensure_fresh(force=True)
    items = {i["sku_id"]: i for i in catalog_service.load_items()}
    # Набору не хватает Да Хун Пао — в продаже, но не в наличии; пачка ТГИ — в наличии.
    assert items["P-TGI-ECO-100"]["in_stock"]
    assert not items["S-ENTRY"]["in_stock"] and not items["S-ENTRY"]["stock_ok"]
    assert not items["B-DANCING-357"]["in_stock"]
    await lot(articles, DHP, 35, code="L-261101-DHP")
    await lot(articles, BLIN, 1, unit="шт", code="L-261101-BLIN")
    await store.ensure_fresh(force=True)
    items = {i["sku_id"]: i for i in catalog_service.load_items()}
    assert items["S-ENTRY"]["in_stock"] and items["B-DANCING-357"]["in_stock"]
    # Сам флаг владельца код не трогает.
    assert await articles.fetchval("select on_sale from shop.catalog where sku_id = 'S-ENTRY'")


async def test_sheet_mode_leaves_the_stock_alone(clean, articles, monkeypatch):
    monkeypatch.setattr(settings, "catalog_source", "sheet")
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 1, "price": 390}])
    assert (await stock.reserve_order(paid.id)).reserved == 0
    assert await stock.write_off(paid.id) == 0
    assert await stock.packing_view(paid.id) == []


async def test_lot_code_and_form_role_checks(articles):
    import asyncpg

    with pytest.raises(asyncpg.RaiseError, match="L-ГГММДД"):
        await lot(articles, TGI, 100, code="партия 1")
    lot_id = await lot(articles, TGI, 100, code="L-261101-TGI")
    with pytest.raises(asyncpg.RaiseError, match="пишет только бот"):
        async with articles.transaction():
            await articles.execute("set local role metabase_fin")
            await articles.execute("insert into shop.stock_moves (lot_id, kind, qty) values ($1, 'списание', 10)", lot_id)
    async with articles.transaction():
        await articles.execute("set local role metabase_fin")
        await articles.execute("insert into shop.stock_moves (lot_id, kind, qty) values ($1, 'порча', 10)", lot_id)


# --- сборка целиком ---------------------------------------------------------------------------


def code(serial: str, gtin: str) -> str:
    return f"01{gtin}21{serial}{GS}93dGVz"


async def test_finish_writes_off_and_shows_lots(clean, articles, monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "token-for-tests")
    await lot(articles, TGI, 1000, code="L-261101-TGI", best_before=TODAY + timedelta(days=400))
    await store.ensure_fresh(force=True)
    paid = await order(clean, [{"name": "Те Гуань Инь", "quantity": 1, "price": 390, "sku_id": "P-TGI-ECO-100"}])
    await stock.reserve_order(paid.id)
    state = await packing.state(paid.id)
    (row,) = state.as_dict()["positions"][0]["lots"]
    assert (row["lot_code"], row["unit"], row["shelf_ok"]) == ("L-261101-TGI", "г", True)
    gtin = "0" + GTIN_A
    assert (await packing.scan(paid.id, code("AAAAAAAAAAAA1", gtin), by="Оля")).ok
    await packing.finish(paid.id, by="Оля")
    reservation = (await reservations(articles, paid.id))[0]
    assert reservation["status"] == "списан" and reservation["pack_best_before"] == TODAY + timedelta(days=400)
    assert (await balance(articles, "L-261101-TGI"))["written_off"] == 100


# --- формы Metabase ---------------------------------------------------------------------------


@pytest.fixture
def stock_forms(monkeypatch):
    import sys
    import types
    from pathlib import Path

    created = []

    def call(method, path, body=None):
        if path == "/dataset":
            return {"data": {"rows": [["L-261101-TGI · Те Гуань Инь ЭКО (XFI0044) · 1000 г"]]}}
        if path == "/action":
            created.append(body)
            return {"id": len(created)}
        if path == "/card":
            return {"id": 800}
        raise AssertionError(f"неожиданный вызов {method} {path}")

    fake = types.ModuleType("mbapi")
    fake.BASE, fake.call = "https://stats.example/api", call
    monkeypatch.setitem(sys.modules, "mbapi", fake)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "analytics" / "metabase"))
    for name in ("fin_build", "dash_build"):
        sys.modules.pop(name, None)
    import dash_build  # noqa: PLC0415

    dash_build.fb.SKUS = [TGI]
    dash_build.stock_forms(1, 9, {"shop.lots": 21}, {})
    yield created
    for name in ("fin_build", "dash_build"):
        sys.modules.pop(name, None)


async def test_stock_forms_run_under_the_form_role(clean, articles, stock_forms):
    from tests.test_catalog_forms import render, run_as_form

    sql = {a["name"]: a["dataset_query"]["native"]["query"] for a in stock_forms if a.get("type") == "query"}
    assert {a["kind"] for a in stock_forms if a.get("type") == "implicit"} == {"row/update"}
    await run_as_form(articles, render(sql["Партия закупки"], {
        "lot_code": "L-261101-TGI", "raw": TGI, "unit": "г", "qty": 1000, "arrival": str(TODAY),
        "best_before": str(TODAY + timedelta(days=700)), "manufacturer": "Anxi Tea Co.", "price": 603.75,
        "discount": 10, "entered_by": "Никита"}))
    created = await articles.fetchrow("select * from shop.lots")
    assert (created["qty"], created["country"], created["discount"]) == (1000, "Китай", Decimal("0.1000"))
    await run_as_form(articles, render(sql["Инвентаризация"], {
        "lot": "L-261101-TGI · x · 1000 г", "fact": 990, "entered_by": "Илья"}))
    await run_as_form(articles, render(sql["Списать сырьё"], {
        "lot": "L-261101-TGI · x · 990 г", "qty": 40, "reason": "порча", "entered_by": "Илья"}))
    b = await balance(articles, "L-261101-TGI")
    assert (b["adjusted"], b["spoiled"], b["on_hand"]) == (-10, 40, 950)


# --- оплата и возврат через уведомления ЮKassa --------------------------------------------------


async def test_paid_webhook_reserves_and_refund_releases(clean, articles, monkeypatch):
    from app.modules.payment import webhook, yookassa_client
    from tests.test_paid_after_close import make_order, paid

    async def nothing(*args, **kwargs):
        return True

    async def register(**kwargs):
        from app.modules.orders import shipping

        return shipping.Registered(ozon_posting="0123-4567-8")

    monkeypatch.setattr("app.messages.client.vk_client.send_message", nothing)
    monkeypatch.setattr(webhook.order_chat, "send", nothing)
    monkeypatch.setattr(webhook.shipping, "register", register)
    await lot(articles, TGI, 1000, code="L-261101-TGI")
    unpaid = await make_order(clean, status="awaiting_payment", payment_id="pay-st", payment_status="pending")
    await orders_repository.register_payment(unpaid.id, "pay-st", attempt=1, status="pending", amount=917.0)
    await webhook.handle_paid(paid("pay-st"))
    assert (await balance(articles, "L-261101-TGI"))["reserved"] == 100
    async def get_refund(refund_id):
        return yookassa_client.Refund(id=refund_id, payment_id="pay-st", status="succeeded", amount=917.0)

    monkeypatch.setattr(webhook.yookassa_client, "get_refund", get_refund)
    await webhook._on_refund("ref-st")
    assert (await balance(articles, "L-261101-TGI"))["reserved"] == 0
