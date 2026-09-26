"""Постоянному клиенту первым предлагаем доставку «как в прошлый раз»."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.modules.orders import conversation, repeat_delivery, state
from app.modules.orders.models import Order

PEER = 9950


async def make_order(db, *, minutes_ago: int, **fields) -> Order:
    values = dict(
        peer_id=PEER, items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 100}],
        items_total=100, delivery_cost=117, total=217, delivery_method="ozon_pvz",
        status="paid", payment_id=f"pay-{minutes_ago}", payment_status="succeeded",
        details={"address": "Краснодар", "ozon_point_id": 437468,
                 "ozon_point_address": "Россия, Краснодарский Край, Краснодар, Ставропольская улица, 230"},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


async def test_last_paid_ozon_point_is_offered(clean):
    order = await make_order(clean, minutes_ago=60)

    last = await repeat_delivery.last_for(PEER)

    assert last is not None and last.order_id == order.id
    text = repeat_delivery.suggestion(last)
    assert "Ставропольская улица, 230" in text
    assert 'method="ozon_pvz", address="Краснодар"' in text
    assert "спроси" in text


async def test_unpaid_canceled_refunded_and_returned_are_skipped(clean):
    await make_order(clean, minutes_ago=10, payment_status="pending", status="awaiting_payment")
    await make_order(clean, minutes_ago=20, status="canceled")
    await make_order(clean, minutes_ago=30, status="refunded")
    await make_order(clean, minutes_ago=40, not_delivered_at=datetime.now(timezone.utc))
    assert await repeat_delivery.last_for(PEER) is None

    good = await make_order(clean, minutes_ago=500)
    assert (await repeat_delivery.last_for(PEER)).order_id == good.id


async def test_cdek_point_and_courier(clean):
    await make_order(
        clean, minutes_ago=5, delivery_method="cdek_pvz",
        details={"address": "Москва", "delivery_point": "MSK123",
                 "delivery_label": "СДЭК, пункт выдачи: Москва, ул. Тверская, 7"},
    )
    last = await repeat_delivery.last_for(PEER)
    assert last.method == "cdek_pvz" and last.place == "Москва, ул. Тверская, 7"

    await make_order(
        clean, minutes_ago=1, delivery_method="cdek_courier",
        details={"address": "Москва, ул. Ленина, 1, кв. 5"},
    )
    last = await repeat_delivery.last_for(PEER)
    assert 'method="cdek_courier", address="Москва, ул. Ленина, 1, кв. 5"' in last.tool_call()


async def test_other_clients_orders_are_not_offered(clean):
    await make_order(clean, minutes_ago=5, peer_id=PEER + 1)
    assert await repeat_delivery.last_for(PEER) is None


async def test_propose_order_offers_the_same_point(clean):
    await make_order(clean, minutes_ago=60)

    result = await conversation._execute_propose_order(
        PEER, {"items": [{"name": "Те Гуань Инь", "quantity": 1}]}
    )

    assert "Черновик заказа создан" in result
    assert "Ставропольская улица, 230" in result
    # Общий совет «первым Ozon, спроси город» не мешает прошлому адресу.
    assert "спроси город" not in result
    await state.clear_draft(PEER)


async def test_new_client_gets_the_usual_advice(clean):
    result = await conversation._execute_propose_order(
        PEER, {"items": [{"name": "Те Гуань Инь", "quantity": 1}]}
    )
    assert "Первым предлагай пункт выдачи Ozon" in result
    await state.clear_draft(PEER)
