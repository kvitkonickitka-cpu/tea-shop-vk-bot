"""Закрывающий чек при вручении: состав, один на заказ, без кодов — нет."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core import worktime
from app.core.config import settings
from app.modules.catalog import service as catalog_service
from app.modules.marking import packing
from app.modules.marking.codes import GS
from app.modules.marking.models import SOLD, MarkingCodeRow
from app.modules.orders import delivery_events, repository as orders_repository
from app.modules.orders.models import Order, OrderPayment
from app.modules.payment import settlement, yookassa_client

GTIN = "04606203099221"
CODE_1 = f"01{GTIN}21SERIAL0000001{GS}93dGVz"
CODE_2 = f"01{GTIN}21SERIAL0000002{GS}93aBc1"


@pytest.fixture
def world(monkeypatch):
    box = {"receipts": [], "keys": [], "manager": [], "client": [], "answer": "pending"}

    async def create_receipt(payload, key):
        box["receipts"].append(payload)
        box["keys"].append(key)
        answer = box["answer"]
        if isinstance(answer, Exception):
            raise answer
        return yookassa_client.Receipt(id=f"rt-{len(box['receipts'])}", status=answer,
                                       payment_id=payload["payment_id"])

    async def to_manager(order, text):
        box["manager"].append(text)

    async def to_client(peer_id, text, random_id=None):
        box["client"].append(text)

    monkeypatch.setattr(settings, "settlement_receipt_enabled", True)
    monkeypatch.setattr(yookassa_client, "create_receipt", create_receipt)
    monkeypatch.setattr(settlement.order_chat, "send", to_manager)
    monkeypatch.setattr(delivery_events.order_chat, "send", to_manager)
    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    monkeypatch.setattr(worktime, "is_quiet", lambda moment=None: False)
    monkeypatch.setattr(catalog_service, "load_items",
                        lambda: [{"name": "Те Гуань Инь", "price": 800, "gtin": GTIN}])
    return box


async def make_order(db, *, packed: bool = True, email: str = "a@b.ru", **fields) -> Order:
    values = dict(
        peer_id=8300, items=[{"name": "Те Гуань Инь", "quantity": 2, "price": 800}],
        items_total=1600, delivery_cost=117, total=1717, delivery_method="ozon_pvz",
        status="confirmed", payment_status=orders_repository.PAID, payment_id="pay-8300",
        ozon_posting="0001-1",
        details={"recipient_email": email, "recipient_name": "Иванов Иван",
                 "delivery_label": "Ozon, пункт выдачи: Ставропольская 230"},
        created_at=datetime.now(timezone.utc),
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.flush()
        session.add(OrderPayment(payment_id=order.payment_id, order_id=order.id, attempt=1,
                                 status="succeeded", amount=1717))
        await session.commit()
    if packed:
        for raw in (CODE_1, CODE_2):
            assert (await packing.scan(order.id, raw, by="Оля")).ok
        await packing.finish(order.id, by="Оля")
    return await orders_repository.by_id(order.id)


async def test_receipt_composition(clean, world):
    order = await make_order(clean)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")

    assert len(world["receipts"]) == 1
    body = world["receipts"][0]
    assert body["type"] == "payment" and body["payment_id"] == "pay-8300"
    assert body["send"] is True and body["internet"] is True and body["timezone"] == 2
    assert body["customer"] == {"full_name": "Иванов Иван", "email": "a@b.ru"}

    packs = [i for i in body["items"] if i["payment_subject"] == "marked"]
    delivery = [i for i in body["items"] if i["payment_subject"] == "service"]
    assert len(packs) == 2 and len(delivery) == 1
    for item in body["items"]:
        assert item["payment_mode"] == "full_payment"
        assert item["measure"] == "piece"
    for item, raw in zip(packs, (CODE_1, CODE_2)):
        assert item["quantity"] == 1
        assert item["amount"] == {"value": "800.00", "currency": "RUB"}
        assert item["mark_mode"] == "0"
        assert item["mark_code_info"]["gs_1m"] == raw
    assert delivery[0]["description"] == "Доставка: Ozon, пункт выдачи: Ставропольская 230"
    assert delivery[0]["amount"]["value"] == "117.00"

    # Сумма — та же, что в первом чеке при оплате.
    first = yookassa_client.receipt_items(order.items, 117, "Ozon, пункт выдачи: Ставропольская 230")
    assert body["settlements"] == [
        {"type": "prepayment", "amount": {"value": "1717.00", "currency": "RUB"}}
    ]
    assert yookassa_client.receipt_total(first) == yookassa_client.receipt_total(body["items"]) == 1717.0

    fresh = await orders_repository.by_id(order.id)
    assert fresh.settlement_receipt_id == "rt-1" and fresh.settlement_receipt_status == "pending"
    # Клиенту сказали, что придёт итоговый чек.
    assert "итоговый чек" in world["client"][-1]


async def test_code_goes_raw_with_escaped_gs(clean, world):
    """Как просит ЮKassa: без base64, GS в JSON — как \\u001d."""
    order = await make_order(clean)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    gs_1m = world["receipts"][-1]["items"][0]["mark_code_info"]["gs_1m"]
    assert gs_1m == CODE_1 and GS in gs_1m
    # То, что уйдёт по сети: httpx сериализует тело стандартным json.
    wire = json.dumps({"gs_1m": gs_1m}, ensure_ascii=False)
    assert "SERIAL0000001\\u001d93dGVz" in wire
    assert GS not in wire


async def test_one_receipt_per_order(clean, world):
    order = await make_order(clean)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    again = await settlement.issue(await orders_repository.by_id(order.id))
    assert again["действий"].startswith("нет")
    await settlement.check()
    assert len(world["receipts"]) == 1


async def test_network_failure_retries_with_the_same_key(clean, world):
    order = await make_order(clean)
    world["answer"] = yookassa_client.YooKassaUnknown("нет ответа")
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    world["answer"] = "pending"
    result = await settlement.check()
    assert result["sent"] == 1
    assert world["keys"][0] == world["keys"][1] == settlement.idempotence_key(order.id, 1)


async def test_rejection_gets_a_new_key(clean, world):
    order = await make_order(clean)
    world["answer"] = yookassa_client.YooKassaError("400 invalid_request")
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    assert any("ЮKassa отказала" in text for text in world["manager"])
    world["answer"] = "pending"
    outcome = await settlement.issue(await orders_repository.by_id(order.id))
    assert outcome["попытка"] == 2
    assert world["keys"][1] == settlement.idempotence_key(order.id, 2) != world["keys"][0]


async def test_delivered_without_codes(clean, world):
    order = await make_order(clean, packed=False)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    assert world["receipts"] == []
    alerts = [t for t in world["manager"] if "нет кодов маркировки" in t]
    assert len(alerts) == 1 and "🚨" in alerts[0]
    assert f"orders/{order.id}/settlement-receipt" in alerts[0]
    # Клиенту про итоговый чек не обещаем.
    assert "итоговый чек" not in world["client"][-1]
    # Таймер не повторяет ни чек, ни уведомление.
    await settlement.check()
    assert world["receipts"] == []
    assert len([t for t in world["manager"] if "нет кодов маркировки" in t]) == 1

    # Исправили: собрали вручённый заказ и отправили командой.
    state = await packing.state(order.id)
    assert state.delivered_without_codes and not state.closed_reason
    for raw in (CODE_1, CODE_2):
        assert (await packing.scan(order.id, raw, by="Оля", manual=True)).ok
    await packing.finish(order.id)
    outcome = await settlement.issue(await orders_repository.by_id(order.id))
    assert outcome["статус"] == "pending" and len(world["receipts"]) == 1


async def test_flag_off_sends_nothing(clean, world, monkeypatch):
    monkeypatch.setattr(settings, "settlement_receipt_enabled", False)
    order = await make_order(clean, packed=False)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    await settlement.check()
    assert world["receipts"] == []
    assert not any("чек" in t for t in world["manager"])


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"details": {"recipient_name": "Иванов"}}, "нет почты"),
        ({"status": "refunded"}, "был возврат"),
    ],
)
async def test_other_blockers(clean, world, fields, expected):
    order = await make_order(clean)
    # Собран честно, а потом что-то случилось: почту стёрли, деньги вернули.
    await orders_repository.set_state(order.id, **fields)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    assert world["receipts"] == []
    assert any(expected in t for t in world["manager"])


async def test_unpaid_order_needs_no_receipt(clean, world):
    order = await make_order(clean, packed=False, payment_status=None)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    assert world["receipts"] == [] and world["manager"] == []


async def test_watch_follows_the_receipt(clean, world, monkeypatch):
    order = await make_order(clean)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")

    answers = {"status": "pending"}

    async def get_receipt(receipt_id):
        return yookassa_client.Receipt(id=receipt_id, status=answers["status"], payment_id="pay-8300")

    monkeypatch.setattr(yookassa_client, "get_receipt", get_receipt)

    later = datetime.now(timezone.utc) + timedelta(hours=25)
    stuck = await settlement.check(later)
    assert stuck["stuck"] == 1
    assert any("pending больше 24 ч" in t for t in world["manager"])

    answers["status"] = "succeeded"
    done = await settlement.check()
    assert done["succeeded"] == 1
    async with clean() as session:
        rows = (await session.execute(
            MarkingCodeRow.__table__.select().where(MarkingCodeRow.order_id == order.id)
        )).all()
    assert {row.status for row in rows} == {SOLD}


async def test_canceled_receipt_is_reported(clean, world, monkeypatch):
    order = await make_order(clean)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")

    async def get_receipt(receipt_id):
        return yookassa_client.Receipt(id=receipt_id, status="canceled", payment_id="pay-8300")

    monkeypatch.setattr(yookassa_client, "get_receipt", get_receipt)
    result = await settlement.check()
    assert result["canceled"] == 1
    assert any("canceled" in t for t in world["manager"])


async def test_manual_command(clean, world, monkeypatch):
    import httpx

    from app.main import app

    monkeypatch.setattr(settings, "internal_api_token", "t0ken")
    order = await make_order(clean)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        early = await http.post(f"/internal/orders/{order.id}/settlement-receipt", content=b"t0ken")
        marked = await http.post(f"/internal/orders/{order.id}/delivered", content=b"t0ken")
        again = await http.post(f"/internal/orders/{order.id}/settlement-receipt", content=b"t0ken")
    assert "ещё не вручён" in early.json()["error"]
    assert marked.json()["отмечено"] is True
    assert again.json()["действий"].startswith("нет")
    assert len(world["receipts"]) == 1
