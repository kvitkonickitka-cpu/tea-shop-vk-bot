"""Задача 4: посылка в пункте выдачи, срок хранения, напоминание за день."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.messages import templates
from app.modules.orders import delivery_events, delivery_watch
from app.modules.orders.models import Order
from tests.test_delivery_statuses import cdek, make_order, world  # noqa: F401

DAY = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)  # 12:00 MSK


class FakeOrder:
    id = 42


def test_texts():
    assert templates.at_pickup_point(
        FakeOrder(), carrier="СДЭК", address="Краснодар, ул. Красная, 1",
        storage_until=date(2026, 10, 5),
    ) == (
        "Посылка по заказу №42 ждёт вас в пункте выдачи СДЭК: Краснодар, ул. Красная, 1.\n"
        "Хранится до 5 октября.\n"
        "Как получить, СДЭК сообщит в SMS. Если что-то пойдёт не так — пишите сюда."
    )
    # Без даты строка выпадает.
    assert templates.at_pickup_point(
        FakeOrder(), carrier="Ozon", address="Краснодар, Ставропольская, 230"
    ) == (
        "Посылка по заказу №42 ждёт вас в пункте выдачи Ozon: Краснодар, Ставропольская, 230.\n"
        "Код для получения — в приложении Ozon, в разделе заказов."
    )
    assert templates.pickup_expiring(FakeOrder(), date(2026, 10, 5)) == (
        "Посылка по заказу №42 ждёт в пункте выдачи до 5 октября. После этого её вернут "
        "нам — заберите, пожалуйста, до этой даты 🙏\n"
        "Если не успеваете — напишите, подскажем, что можно сделать."
    )


def test_read_cdek_at_pickup():
    seen = delivery_watch.read_cdek(cdek("RECEIVED_AT_SHIPMENT_WAREHOUSE", "ACCEPTED_AT_PICK_UP_POINT"))
    assert seen.at_pickup and seen.with_carrier and not seen.postamat
    assert seen.storage_until is None
    postamat = delivery_watch.read_cdek(cdek("RECEIVED_AT_SHIPMENT_WAREHOUSE", "POSTOMAT_POSTED"))
    assert postamat.at_pickup and postamat.postamat
    # Уже вручена — не «ждёт».
    done = delivery_watch.read_cdek(cdek("ACCEPTED_AT_PICK_UP_POINT", "DELIVERED"))
    assert not done.at_pickup and done.delivered


def test_storage_date_is_read_only_when_given():
    data = cdek("ACCEPTED_AT_PICK_UP_POINT")
    data["entity"]["keep_free_until"] = "2026-10-05"
    seen = delivery_watch.read_cdek(data)
    assert seen.storage_until == date(2026, 10, 5)
    assert "keep_free_until=2026-10-05" in seen.storage_fields

    ozon = delivery_watch.read_ozon(
        {"status": "in_delivery_point", "status_changed_at": "2026-09-25T08:00:00Z",
         "storage_expiration_date": "2026-10-01T20:59:59Z"},
        handed_over=True,
    )
    # 20:59 UTC — это 23:59 по Москве, дата та же.
    assert ozon.at_pickup and ozon.storage_until == date(2026, 10, 1)
    plain = delivery_watch.read_ozon({"status": "in_delivery_point"}, handed_over=True)
    assert plain.at_pickup and plain.storage_until is None


async def test_handover_and_pickup_in_one_poll_send_one_message(clean, world, monkeypatch):
    order = await make_order(clean, details={
        "recipient_email": "a@b.ru", "delivery_label": "СДЭК, пункт выдачи: Краснодар, ул. Красная, 1",
    })
    data = cdek("CREATED", "RECEIVED_AT_SHIPMENT_WAREHOUSE", "ACCEPTED_AT_PICK_UP_POINT")
    data["entity"]["keep_free_until"] = "2026-10-05"

    async def order_state(uuid):
        return data

    monkeypatch.setattr(delivery_watch.cdek_client, "order_state", order_state)
    result = await delivery_watch.check_deliveries(DAY)
    assert result["at_pickup"] == 1 and result["handed_over"] == 0
    assert len(world["client"]) == 1
    assert world["client"][0].startswith(
        f"Посылка по заказу №{order.id} ждёт вас в пункте выдачи СДЭК: Краснодар, ул. Красная, 1."
    )
    assert "Хранится до 5 октября." in world["client"][0]

    async with clean() as session:
        row = await session.get(Order, order.id)
    assert row.handed_over_at is not None and row.storage_until == date(2026, 10, 5)

    # Следующий опрос ничего не повторяет.
    again = await delivery_watch.check_deliveries(DAY + timedelta(hours=2))
    assert again["at_pickup"] == 0 and len(world["client"]) == 1


async def test_ozon_at_pickup_without_date(clean, world, monkeypatch):
    order = await make_order(
        clean, cdek_uuid=None, ozon_posting="0001-1", delivery_method="ozon_pvz",
        details={"recipient_email": "a@b.ru", "ozon_point_address": "Краснодар, Ставропольская, 230"},
    )

    async def posting_info(number):
        return {"status": "in_delivery_point",
                "status_changed_at": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr(delivery_watch.ozon_client, "posting_info", posting_info)
    await delivery_watch.check_deliveries(DAY)
    assert world["client"] == [
        f"Посылка по заказу №{order.id} ждёт вас в пункте выдачи Ozon: Краснодар, Ставропольская, 230.\n"
        "Код для получения — в приложении Ozon, в разделе заказов."
    ]


async def test_night_pickup_is_told_in_the_morning(clean, world, monkeypatch):
    order = await make_order(clean)
    monkeypatch.setattr(delivery_events.worktime, "is_quiet", lambda moment=None: True)
    assert await delivery_events.record(order.id, delivery_events.AT_PICKUP, source="тест")
    assert world["client"] == []
    monkeypatch.setattr(delivery_events.worktime, "is_quiet", lambda moment=None: False)
    assert await delivery_events.tell_pending_clients(datetime.now(timezone.utc)) == 1
    assert "ждёт вас в пункте выдачи СДЭК" in world["client"][0]


async def test_storage_reminder_once_and_only_with_date(clean, world):
    now = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)  # 12:00 MSK, за день до 5-го
    with_date = await make_order(
        clean, at_pickup_at=now - timedelta(days=5), handed_over_at=now - timedelta(days=6),
        storage_until=date(2026, 10, 5),
    )
    await make_order(clean, peer_id=8101, cdek_uuid="uuid-2",
                     at_pickup_at=now - timedelta(days=5), handed_over_at=now - timedelta(days=6))
    # Забрали — не напоминаем.
    await make_order(clean, peer_id=8102, cdek_uuid="uuid-3", at_pickup_at=now - timedelta(days=5),
                     storage_until=date(2026, 10, 5), delivered_at=now - timedelta(days=1))
    # Рано: до конца хранения ещё три дня.
    await make_order(clean, peer_id=8103, cdek_uuid="uuid-4", at_pickup_at=now - timedelta(days=2),
                     storage_until=date(2026, 10, 7))

    assert await delivery_events.remind_storage_ending(now) == 1
    assert world["client"] == [templates.pickup_expiring(with_date, date(2026, 10, 5))]
    assert await delivery_events.remind_storage_ending(now + timedelta(hours=3)) == 0
