"""Аналитика, А4: схема analytics — что видит DataLens."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.client_key import client_key
from app.core.config import settings
from app.messages import funnel
from app.messages.models import ClientNotice, ClientPreference
from app.modules.analytics import service as analytics, views
from app.modules.orders.models import Order, OrderPayment, OrderRating

REAL, OTHER, TESTER = 7101, 7102, 7103
T0 = datetime(2026, 10, 1, 21, 30, tzinfo=timezone.utc)  # 2 октября по Москве
RECIPIENT = {"recipient_name": "Иванов Иван", "recipient_phone": "+79001234567",
             "recipient_email": "ivanov@mail.ru", "address": "Москва, Ленина 5"}


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setattr(settings, "client_key_secret", "s3cret")
    monkeypatch.setattr(settings, "test_vk_ids", str(TESTER))
    monkeypatch.setattr(analytics, "_test_ids", None)


async def order(db, peer_id, *, created_at, items=None, details=None, **fields) -> Order:
    values = dict(
        peer_id=peer_id, items=items or [{"name": "Те Гуань Инь 100 г", "quantity": 2, "price": 900}],
        items_total=1800, delivery_cost=121, total=1921, delivery_method="ozon_pvz", status="confirmed",
        payment_status="succeeded", details={**RECIPIENT, **(details or {})}, created_at=created_at,
    )
    values.update(fields)
    async with db() as session:
        row = Order(**values)
        session.add(row)
        await session.commit()
        return row


async def rows(db, view: str, order_by: str) -> list[dict]:
    async with db() as session:
        result = await session.execute(text(f"select * from analytics.{view} order by {order_by}"))
        return [dict(row._mapping) for row in result]


@pytest.fixture
async def shop(clean):
    for peer, ref in ((REAL, "autumn"), (OTHER, None), (TESTER, None)):
        await analytics.ensure_client(peer, ref=ref, ref_source="post" if ref else None)
    first = await order(clean, REAL, created_at=T0, paid_at=T0 + timedelta(minutes=5),
                        delivered_at=T0 + timedelta(days=5),
                        details={"origin": "take", "upsell_offered": True, "upsell_item": "Габа"})
    second = await order(
        clean, REAL, created_at=T0 + timedelta(days=30), status="awaiting_payment", payment_status="pending",
        items=[{"name": "Да Хун Пао 50 г", "quantity": 1, "price": 1500},
               {"name": "Габа", "quantity": 2, "price": 1100}],
        items_total=3700, delivery_cost=0, total=3700,
        details={"origin": "repeat", "carrier_delivery_cost": 230, "upsell_offered": True, "upsell_item": "Габа"},
    )
    storefront = await order(clean, OTHER, created_at=T0 + timedelta(days=1), delivery_method="cdek_courier",
                             details={"vk_order_id": 55}, status="refunded")
    tester = await order(clean, TESTER, created_at=T0)
    test_shop = await order(clean, OTHER, created_at=T0 + timedelta(days=2), is_test=True)
    async with clean() as session:
        session.add_all([
            OrderPayment(payment_id="p1", order_id=first.id, attempt=1, status="canceled", amount=1921),
            OrderPayment(payment_id="p2", order_id=first.id, attempt=2, status="succeeded", amount=1921,
                         payment_method="sbp", income_amount=1880.5),
            OrderRating(order_id=first.id, peer_id=REAL, rating="great", source="button"),
            ClientNotice(ref=f"order:{storefront.id}", event_type="paid", peer_id=OTHER,
                         sent_at=T0 + timedelta(days=1, minutes=3), attempts=1),
            ClientPreference(peer_id=OTHER, marketing_opt_out=True, opted_out_at=T0 + timedelta(days=3)),
        ])
        await session.commit()
    await analytics.sync()
    return {"first": first, "second": second, "storefront": storefront, "tester": tester, "test_shop": test_shop}


async def test_orders(clean, shop):
    result = await rows(clean, "v_orders", "order_id")
    ids = [row["order_id"] for row in result]
    assert ids == sorted([shop["first"].id, shop["second"].id, shop["storefront"].id])  # без тестовых

    first, second, storefront = (next(r for r in result if r["order_id"] == shop[k].id)
                                 for k in ("first", "second", "storefront"))
    assert first["client_key"] == client_key(REAL) and first["ref"] == "autumn" and first["ref_source"] == "post"
    assert str(first["created_date_msk"]) == "2026-10-02"
    assert (first["client_order_number"], first["client_paid_order_number"]) == (1, 1)
    assert (second["client_order_number"], second["client_paid_order_number"]) == (2, None)
    assert first["status_label"] == "Оплачен, передаётся перевозчику" and first["is_paid"] and first["is_delivered"]
    assert (first["channel"], first["channel_label"]) == ("dialog", "Диалог")
    assert (second["channel"], storefront["channel"]) == ("repeat", "storefront")
    assert (first["carrier"], first["delivery_type"]) == ("ozon", "pickup")
    assert (storefront["carrier"], storefront["delivery_type"]) == ("cdek", "courier")
    assert first["payment_attempts"] == 2 and first["payment_method"] == "sbp"
    assert float(first["income_amount"]) == 1880.5
    assert first["rating_label"] == "Очень понравился" and not first["has_review"]
    assert first["items_count"] == 2 and not first["upsell_accepted"] and first["upsell_offered"]
    # Выше порога: клиенту 0, перевозчику 230 — платит магазин.
    assert second["free_delivery"] and float(second["carrier_delivery_cost"]) == 230
    assert second["upsell_accepted"] and float(second["total"]) == 3700
    assert not first["free_delivery"] and float(first["carrier_delivery_cost"]) == 121
    # Старый заказ без отметки оплаты — время сообщения «оплачено».
    assert storefront["paid_at"] == T0 + timedelta(days=1, minutes=3) and storefront["is_refunded"]


async def test_order_items(clean, shop):
    result = await rows(clean, "v_order_items", "order_id, line_no")
    assert {row["order_id"] for row in result} == {shop["first"].id, shop["second"].id, shop["storefront"].id}
    lines = [(r["product"], r["pack"], r["quantity"], float(r["price"]), float(r["amount"]))
             for r in result if r["order_id"] == shop["second"].id]
    assert lines == [("Да Хун Пао", "50 г", 1, 1500.0, 1500.0), ("Габа", None, 2, 1100.0, 2200.0)]


async def test_clients(clean, shop):
    result = {row["client_key"]: row for row in await rows(clean, "v_clients", "client_key")}
    assert set(result) == {client_key(REAL), client_key(OTHER)}
    real, other = result[client_key(REAL)], result[client_key(OTHER)]
    assert (real["orders_count"], real["paid_orders_count"], float(real["revenue"])) == (2, 1, 1921.0)
    # Возвращённый заказ в выручку не идёт, тестовый заказ — вовсе не заказ.
    assert (other["orders_count"], other["paid_orders_count"], float(other["revenue"])) == (1, 1, 0.0)
    assert other["opted_out"] and not other["unreachable"] and other["ref"] is None


async def test_funnel_events_and_touches(clean, shop):
    first = shop["first"]
    await funnel.record(REAL, "dialog_start", at=T0 - timedelta(hours=1))
    await funnel.record(REAL, "button:take", source_=funnel.BUTTON, at=T0 - timedelta(minutes=50))
    await funnel.record(REAL, "payment_succeeded", order_id=first.id, source_=funnel.YOOKASSA, at=T0,
                        attempt=2, amount=1921.0, income=1880.5, method="sbp")
    await funnel.record(TESTER, "dialog_start", at=T0)
    await funnel.record(OTHER, "payment_succeeded", order_id=shop["test_shop"].id, at=T0)
    # Касание, нажатие под ним и заказ — засчитываются ему; второе касание — без ответа.
    sent = T0 + timedelta(days=25)
    await funnel.record(REAL, "touch:repeat_nudge", order_id=first.id, at=sent)
    await funnel.record(REAL, "button:repeat", order_id=first.id, at=sent + timedelta(hours=1), touch="repeat_nudge")
    await funnel.record(REAL, "touch_order", order_id=shop["second"].id, at=sent + timedelta(days=5),
                        touch="repeat_nudge")
    await funnel.record(REAL, "touch:reactivation", order_id=shop["second"].id, at=sent + timedelta(days=6))

    events = await rows(clean, "v_funnel_events", "event_id")
    assert [(e["event"], e["event_kind"], e["event_label"], e["funnel_order"]) for e in events[:3]] == [
        ("dialog_start", "dialog_start", "Начало диалога", 10),
        ("button:take", "button", "Нажата «Взять»", 25),
        ("payment_succeeded", "payment_succeeded", "Оплата прошла", 70),
    ]
    paid = events[2]
    assert (paid["source_label"], paid["attempt"], float(paid["amount"]), paid["payment_method"]) == (
        "ЮKassa", 2, 1921.0, "sbp")
    assert events[1]["button_action"] == "take" and events[1]["source"] == "button"
    assert all(e["client_key"] == client_key(REAL) for e in events)  # без тестовых клиента и заказа

    touches = await rows(clean, "v_touches", "touch_id")
    assert [(t["touch"], t["pressed"], t["pressed_action"], t["ordered_7d"], t["new_order_id"], t["opted_out_2d"])
            for t in touches] == [
        ("repeat_nudge", True, "repeat", True, shop["second"].id, False),
        ("reactivation", False, None, False, None, False),
    ]
    assert touches[0]["touch_label"] == "«Повторить заказ?»"


async def test_no_personal_data_anywhere(clean, shop):
    await funnel.record(REAL, "recipient_set", order_id=shop["first"].id)
    for view in views.VIEWS:
        async with clean() as session:
            dump = str((await session.execute(text(f"select * from analytics.{view.name}"))).all())
        for value in (*RECIPIENT.values(), str(REAL), str(OTHER)):
            assert value not in dump, (view.name, value)


async def test_every_view_and_column_is_described(clean):
    async with clean() as session:
        missing = (await session.execute(text(
            "select c.table_name, c.column_name from information_schema.columns c"
            " join pg_class t on t.relname = c.table_name"
            " join pg_namespace n on n.oid = t.relnamespace and n.nspname = c.table_schema"
            " where c.table_schema = 'analytics'"
            " and col_description(t.oid, c.ordinal_position) is null"
        ))).all()
        views_without = (await session.execute(text(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'analytics' and obj_description(c.oid, 'pg_class') is null"
        ))).all()
    assert missing == [] and views_without == []


async def test_dims_cover_every_event_the_bot_writes():
    """Всякое событие, которое бот пишет, — с подписью в dim_event."""
    import pathlib
    import re

    codes = {row[0] for row in views.EVENTS}
    written = set()
    for path in pathlib.Path("app").rglob("*.py"):
        for match in re.finditer(r'funnel\.record\(\s*[\w.]+,\s*f?"([a-z_0-9]+)', path.read_text()):
            written.add(match.group(1))
    written -= {"touch", "button"}  # f-строки button:{…} и touch:{…} — общими кодами
    assert written - codes == set()
