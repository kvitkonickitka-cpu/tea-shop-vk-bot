"""Возврат до закрывающего чека: чек по данным платежа, коды — в наличие."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.modules.catalog import service as catalog_service
from app.modules.marking import packing
from app.modules.marking.codes import GS
from app.modules.marking.models import ASSIGNED, IN_STOCK, MarkingCodeRow
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order, OrderPayment
from app.modules.payment import webhook, yookassa_client

GTIN = "04606203099221"
CODE = f"01{GTIN}21SERIAL0000009{GS}93dGVz"


async def test_full_refund_carries_no_receipt(monkeypatch):
    sent = {}

    async def fake_call(method, path, payload=None, key=""):
        sent.update(payload)
        return {"id": "ref-1", "payment_id": payload["payment_id"], "status": "succeeded",
                "amount": payload["amount"]}

    monkeypatch.setattr(yookassa_client, "_call", fake_call)
    await yookassa_client.create_refund(
        payment_id="pay-2", amount=1717, items=[{"name": "Чай", "quantity": 2, "price": 800}],
        delivery_cost=117, delivery_label="Ozon", email="a@b.ru", phone="79181234567",
        full_name="Иванов", full=True,
    )
    assert sent == {"payment_id": "pay-2", "amount": {"value": "1717.00", "currency": "RUB"}}


async def test_partial_refund_repeats_the_prepayment_items(monkeypatch):
    sent = {}

    async def fake_call(method, path, payload=None, key=""):
        sent.update(payload)
        return {"id": "ref-1", "payment_id": "pay-2", "status": "pending", "amount": payload["amount"]}

    monkeypatch.setattr(yookassa_client, "_call", fake_call)
    await yookassa_client.create_refund(
        payment_id="pay-2", amount=800, items=[{"name": "Чай", "quantity": 1, "price": 800}],
        delivery_cost=0, delivery_label="", email="a@b.ru", phone="79181234567",
        full_name="Иванов",
    )
    item = sent["receipt"]["items"][0]
    assert item["payment_mode"] == "full_prepayment" and "mark_code_info" not in item
    assert sent["receipt"]["customer"] == {"full_name": "Иванов", "email": "a@b.ru"}


@pytest.fixture
def world(monkeypatch):
    box = {"manager": [], "client": []}

    async def to_client(peer_id, text, random_id=None):
        box["client"].append(text)

    async def to_manager(order, text):
        box["manager"].append(text)

    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    monkeypatch.setattr(webhook.order_chat, "send", to_manager)
    monkeypatch.setattr(catalog_service, "load_items",
                        lambda: [{"name": "Те Гуань Инь", "price": 800, "gtin": GTIN}])

    async def get_refund(refund_id):
        return yookassa_client.Refund(id=refund_id, payment_id="pay-9", status="succeeded", amount=917)

    monkeypatch.setattr(webhook.yookassa_client, "get_refund", get_refund)
    return box


async def packed_order(db, payment_id: str = "pay-9", **later) -> Order:
    """Собранный заказ; `later` — что с ним случилось после сборки."""
    async with db() as session:
        order = Order(
            peer_id=8400, items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
            items_total=800, delivery_cost=117, total=917, delivery_method="ozon_pvz",
            status="confirmed", payment_status=orders_repository.PAID, payment_id=payment_id,
            details={"recipient_email": "a@b.ru"}, created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        await session.flush()
        session.add(OrderPayment(payment_id=payment_id, order_id=order.id, attempt=1,
                                 status="succeeded", amount=917))
        await session.commit()
    assert (await packing.scan(order.id, CODE)).ok
    await packing.finish(order.id)
    if later:
        await orders_repository.set_state(order.id, **later)
    return order


async def code_row(db) -> MarkingCodeRow:
    async with db() as session:
        return (await session.execute(
            MarkingCodeRow.__table__.select()
        )).one()


async def test_refund_before_delivery_frees_the_codes(clean, world):
    order = await packed_order(clean, not_delivered_at=datetime.now(timezone.utc))
    await webhook.handle({"event": "refund.succeeded", "object": {"id": "ref-9"}})
    row = await code_row(clean)
    assert row.status == IN_STOCK and row.order_id is None
    assert any("освобождены: 1" in text for text in world["manager"])
    # Пачка снова годится для другого заказа.
    other = await packed_order(clean, payment_id="pay-10")
    assert other.id != order.id


async def test_refund_after_delivery_keeps_the_codes(clean, world):
    await packed_order(clean, delivered_at=datetime.now(timezone.utc),
                       settlement_receipt_id="rt-1", settlement_receipt_status="succeeded")
    await webhook.handle({"event": "refund.succeeded", "object": {"id": "ref-9"}})
    row = await code_row(clean)
    assert row.status == ASSIGNED and row.order_id is not None
    assert any("после закрывающего чека" in text for text in world["manager"])
