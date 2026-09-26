"""Статусы доставки: передано, вручено, не вручено — один раз и по делу."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import worktime
from app.messages import templates
from app.modules.orders import delivery_events, delivery_watch, repository as orders_repository
from app.modules.orders.models import Order

PEER = 8100
# Днём по Москве: 12:00 MSK = 09:00 UTC.
DAY = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc)  # 01:00 MSK


async def make_order(db, **fields) -> Order:
    values = dict(
        peer_id=PEER, items=[{"name": "Те Гуань Инь", "quantity": 2, "price": 800}],
        items_total=1600, delivery_cost=117, total=1717, delivery_method="cdek_pvz",
        status="cdek_registered", payment_status=orders_repository.PAID,
        payment_id="pay-1", cdek_uuid="uuid-1",
        details={"recipient_email": "a@b.ru", "order_key": "vk8100-1"},
        created_at=datetime.now(timezone.utc),
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.commit()
        return order


@pytest.fixture
def world(monkeypatch):
    box = {"client": [], "manager": []}

    async def to_client(peer_id, text, random_id=None):
        box["client"].append(text)

    async def to_manager(order, text):
        box["manager"].append(text)

    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    monkeypatch.setattr(delivery_events.order_chat, "send", to_manager)
    monkeypatch.setattr(delivery_watch.order_chat, "send", to_manager)
    monkeypatch.setattr(worktime, "is_quiet", lambda moment=None: False)
    return box


def cdek(*codes: str, number: str = "1100285492", days_ago: float = 0) -> dict:
    """Ответ СДЭКа: статусы по часу друг за другом, последний — `days_ago` назад."""
    last = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "entity": {
            "cdek_number": number,
            "statuses": [
                {"code": code, "name": code.lower(),
                 "date_time": (last - timedelta(hours=len(codes) - 1 - i)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
                for i, code in enumerate(codes)
            ],
        }
    }


def test_read_cdek():
    assert not delivery_watch.read_cdek(cdek("CREATED", "ACCEPTED")).with_carrier
    seen = delivery_watch.read_cdek(cdek("CREATED", "RECEIVED_AT_SHIPMENT_WAREHOUSE"))
    assert seen.with_carrier and not seen.delivered
    assert "RECEIVED_AT_SHIPMENT_WAREHOUSE" in seen.status
    done = delivery_watch.read_cdek(cdek("CREATED", "RECEIVED_AT_SHIPMENT_WAREHOUSE", "DELIVERED"))
    assert done.delivered and done.with_carrier and done.cdek_number == "1100285492"
    back = delivery_watch.read_cdek(cdek("RECEIVED_AT_SHIPMENT_WAREHOUSE", "NOT_DELIVERED"))
    assert back.not_delivered and not back.delivered
    assert delivery_watch.read_cdek(cdek("CREATED", "REMOVED")).trouble


def test_read_ozon():
    assert delivery_watch.read_ozon({"status": "on_way"}, handed_over=False).with_carrier
    assert delivery_watch.read_ozon({"status": "delivered"}, handed_over=True).delivered
    # Отмена после приёмки — возврат, до приёмки — заминка.
    assert delivery_watch.read_ozon({"status": "canceled"}, handed_over=True).not_delivered
    early = delivery_watch.read_ozon({"status": "canceled"}, handed_over=False)
    assert early.trouble and not early.not_delivered
    assert delivery_watch.read_ozon({"status": "not_accepted_to_delivery"}, handed_over=False).trouble


async def test_event_is_recorded_once(clean, world):
    order = await make_order(clean)
    first = await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    again = await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    assert first is not None and again is None
    # Вручение подразумевает передачу.
    fresh = await orders_repository.by_id(order.id)
    assert fresh.delivered_at is not None and fresh.handed_over_at is not None
    # Не вручить уже вручённое нельзя.
    assert await delivery_events.record(order.id, delivery_events.NOT_DELIVERED, source="тест") is None
    assert len([t for t in world["client"] if "вручён" in t]) == 1


async def test_watch_walks_the_parcel(clean, world, monkeypatch):
    order = await make_order(clean)
    answers = iter([
        cdek("CREATED", "RECEIVED_AT_SHIPMENT_WAREHOUSE"),
        cdek("CREATED", "RECEIVED_AT_SHIPMENT_WAREHOUSE", "DELIVERED"),
    ])

    async def order_state(uuid):
        return next(answers)

    monkeypatch.setattr(delivery_watch.cdek_client, "order_state", order_state)

    first = await delivery_watch.check_deliveries(DAY)
    assert first["handed_over"] == 1
    assert "принята СДЭКом" in world["client"][-1]
    assert "1100285492" in world["client"][-1]

    # Через час — вручено.
    second = await delivery_watch.check_deliveries(DAY + timedelta(minutes=61))
    assert second["delivered"] == 1
    assert "вручён" in world["client"][-1]

    # Вручённый заказ больше не опрашивается.
    third = await delivery_watch.check_deliveries(DAY + timedelta(minutes=200))
    assert third["checked"] == 0


async def test_watch_respects_the_interval(clean, world, monkeypatch):
    await make_order(clean)
    calls = []

    async def order_state(uuid):
        calls.append(uuid)
        return cdek("CREATED")

    monkeypatch.setattr(delivery_watch.cdek_client, "order_state", order_state)
    await delivery_watch.check_deliveries(DAY)
    await delivery_watch.check_deliveries(DAY + timedelta(minutes=5))
    assert len(calls) == 1


async def test_not_delivered_tells_manager_and_client(clean, world, monkeypatch):
    order = await make_order(clean, cdek_uuid=None, ozon_posting="0001-1", delivery_method="ozon_pvz",
                             handed_over_at=DAY - timedelta(days=5))

    async def posting_info(number):
        return {"status": "canceled"}

    monkeypatch.setattr(delivery_watch.ozon_client, "posting_info", posting_info)
    result = await delivery_watch.check_deliveries(DAY)
    assert result["not_delivered"] == 1
    assert any("не вручён" in text for text in world["manager"])
    assert "возвращается к нам" in world["client"][-1]
    fresh = await orders_repository.by_id(order.id)
    assert fresh.not_delivered_at is not None


async def test_night_news_waits_for_morning(clean, world, monkeypatch):
    monkeypatch.setattr(worktime, "is_quiet", lambda moment=None: True)
    order = await make_order(clean)
    await delivery_events.record(order.id, delivery_events.HANDED_OVER, source="тест")
    assert world["client"] == []

    monkeypatch.setattr(worktime, "is_quiet", lambda moment=None: False)
    told = await delivery_events.tell_pending_clients(DAY + timedelta(hours=2))
    assert told == 1
    assert await delivery_events.tell_pending_clients(DAY + timedelta(hours=3)) == 0
    assert len(world["client"]) == 1


async def test_unpaid_order_gets_no_news(clean, world):
    order = await make_order(clean, payment_status=None)
    await delivery_events.record(order.id, delivery_events.HANDED_OVER, source="тест")
    assert world["client"] == []


async def test_trouble_is_reported_once(clean, world, monkeypatch):
    await make_order(clean)

    async def order_state(uuid):
        return cdek("CREATED", "REMOVED")

    monkeypatch.setattr(delivery_watch.cdek_client, "order_state", order_state)
    await delivery_watch.check_deliveries(DAY)
    await delivery_watch.check_deliveries(DAY + timedelta(hours=2))
    assert len([t for t in world["manager"] if "заминка" in t]) == 1


def test_templates_read_well():
    order = type("O", (), {"id": 5})()
    assert "Трек-номер: 11" in templates.handed_over(order, carrier="СДЭКом", number="11", tracking_url="u")
    assert "a@b.ru" in templates.delivered(order, receipt_email="a@b.ru")
    assert "чек" not in templates.delivered(order)


async def test_old_delivery_is_marked_silently(clean, world, monkeypatch):
    """Первый опрос старого заказа: вручено три недели назад — клиенту не пишем."""
    order = await make_order(clean, created_at=datetime.now(timezone.utc) - timedelta(days=25))

    async def order_state(uuid):
        return cdek("CREATED", "RECEIVED_AT_SHIPMENT_WAREHOUSE", "DELIVERED", days_ago=21)

    monkeypatch.setattr(delivery_watch.cdek_client, "order_state", order_state)
    result = await delivery_watch.check_deliveries()
    assert result["delivered"] == 1
    assert world["client"] == []
    fresh = await orders_repository.by_id(order.id)
    # Отметка — временем СДЭКа, а не временем опроса.
    assert datetime.now(timezone.utc) - fresh.delivered_at > timedelta(days=20)
    assert await delivery_events.tell_pending_clients() == 0


def test_carrier_times_are_read():
    seen = delivery_watch.read_cdek(cdek("CREATED", "RECEIVED_AT_SHIPMENT_WAREHOUSE", "DELIVERED", days_ago=2))
    assert seen.handed_over_at < seen.finished_at
    ozon = delivery_watch.read_ozon(
        {"status": "delivered", "status_changed_at": "2026-09-20T10:00:00Z"}, handed_over=True
    )
    assert ozon.finished_at == datetime(2026, 9, 20, 10, tzinfo=timezone.utc)


async def test_deleted_cdek_order_is_reported_once(clean, world, monkeypatch):
    await make_order(clean)
    calls = []

    async def order_state(uuid):
        calls.append(uuid)
        raise RuntimeError("HTTP 400; {'errors': [{'code': 'v2_entity_not_found'}]}")

    monkeypatch.setattr(delivery_watch.cdek_client, "order_state", order_state)
    await delivery_watch.check_deliveries(DAY)
    await delivery_watch.check_deliveries(DAY + timedelta(hours=3))
    assert len(calls) == 1
    assert len([t for t in world["manager"] if "не найден" in t]) == 1


async def test_ozon_canceled_before_handover_is_polled_once(clean, world, monkeypatch):
    """Отменённое до передачи отправление — конец: менеджеру один раз, опрос прекращается."""
    await make_order(clean, cdek_uuid=None, ozon_posting="0002-1", delivery_method="ozon_pvz")
    calls = []

    async def posting_info(number):
        calls.append(number)
        return {"status": "canceled"}

    monkeypatch.setattr(delivery_watch.ozon_client, "posting_info", posting_info)
    first = await delivery_watch.check_deliveries(DAY)
    assert first["trouble"] == 1 and first["not_delivered"] == 0

    later = DAY + timedelta(hours=3)
    await delivery_watch.check_deliveries(later)
    assert calls == ["0002-1"]
    assert len([t for t in world["manager"] if "заминка" in t]) == 1
