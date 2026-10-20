"""Пакет C, вторая часть: касание по брошенному заказу (C3), напоминания о посылке в
пункте (C4), подсказка до бесплатной доставки (C5). Всё — за флагами, allowlist."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.core import flags
from app.core.config import settings
from app.messages import funnel
from app.messages.models import ClientNotice, FunnelEvent
from app.modules.analytics import service as analytics
from app.modules.catalog import service as catalog_service
from app.modules.dialog import history as dialog_history
from app.modules.orders import abandoned, conversation, pickup_reminders, state
from app.modules.orders.models import Order
from app.modules.orders.state import OrderDraft

PEER = 9961
NOON = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)  # 12:00 МСК


@pytest.fixture
def tester(monkeypatch):
    monkeypatch.setattr(settings, "test_vk_ids", str(PEER))
    monkeypatch.setattr(analytics, "_test_ids", None)
    sent = []

    async def to_client(peer_id, text_, random_id=None, **kwargs):
        sent.append(text_)

    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    return sent


async def on(*names):
    for name in names:
        await flags.set_mode(name, flags.ALLOWLIST, by="test")


async def events(db, peer=PEER) -> list[str]:
    async with db() as session:
        return list((await session.execute(select(FunnelEvent.event).where(FunnelEvent.peer_id == peer)
                                           .order_by(FunnelEvent.id))).scalars())


async def abandoned_draft(db, *, bot_said_hours_ago: float, order_id=None):
    draft = OrderDraft(items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 1500}], items_total=1500,
                       delivery_method="ozon_pvz", delivery_label="Ozon, пункт выдачи: Краснодар, Красная, 176",
                       delivery_cost=121, stage="awaiting_confirmation",
                       details={"started_at": (NOON - timedelta(hours=30)).isoformat(), "version": 3,
                                **({"order_id": order_id} if order_id else {})})
    await state.set_draft(PEER, draft)
    await dialog_history.append_message(PEER, "user", "беру")
    await dialog_history.append_message(PEER, "assistant", "Пришлите ФИО, телефон и почту")
    async with db() as session:
        await session.execute(text(
            "update conversation_messages set created_at = :t where peer_id = :p"),
            {"t": NOON - timedelta(hours=bot_said_hours_ago), "p": PEER})
        await session.execute(text("update order_drafts set updated_at = :t where peer_id = :p"),
                              {"t": NOON - timedelta(hours=bot_said_hours_ago), "p": PEER})
        await session.commit()


async def test_abandoned_touch_once_with_checkout_button(clean, tester):
    await on(flags.ABANDONED_TOUCH)
    await abandoned_draft(clean, bot_said_hours_ago=21)
    result = await abandoned.check(NOON)
    assert result["отправлено"] == 1
    assert tester[0].startswith("Ваш заказ сохранился:\n• Те Гуань Инь 100 г × 1")
    assert "Итого: 1621 ₽" in tester[0] and "Оформить" in tester[0]
    assert (await abandoned.check(NOON + timedelta(hours=1)))["отправлено"] == 0
    assert await events(clean) == ["abandoned_touch"]
    # Оплата в течение 7 дней — засчитана касанию.
    await funnel.record(PEER, "payment_succeeded", order_id=7)
    assert await events(clean) == ["abandoned_touch", "payment_succeeded", "abandoned_touch_order"]


async def test_abandoned_touch_waits_and_respects_rules(clean, tester):
    await on(flags.ABANDONED_TOUCH)
    await abandoned_draft(clean, bot_said_hours_ago=5)
    assert (await abandoned.check(NOON))["кандидатов"] == 0
    # Ночью — никогда.
    await abandoned_draft(clean, bot_said_hours_ago=21)
    assert (await abandoned.check(datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)))["skipped"]
    # Продающее касание за последние 3 дня — пауза.
    async with clean() as session:
        session.add(ClientNotice(ref="order:1", event_type="repeat_nudge", peer_id=PEER,
                                 sent_at=NOON - timedelta(days=1)))
        await session.commit()
    assert (await abandoned.check(NOON))["отправлено"] == 0


async def test_abandoned_touch_skips_paid_order_and_flag_off(clean, tester):
    await abandoned_draft(clean, bot_said_hours_ago=21)
    assert (await abandoned.check(NOON))["отправлено"] == 0  # флаг выключен
    await on(flags.ABANDONED_TOUCH)
    async with clean() as session:
        order = Order(peer_id=PEER, items=[], items_total=1500, delivery_cost=121, total=1621,
                      delivery_method="ozon_pvz", status="confirmed", payment_status="succeeded", details={})
        session.add(order)
        await session.commit()
        order_id = order.id
    await abandoned_draft(clean, bot_said_hours_ago=21, order_id=order_id)
    assert (await abandoned.check(NOON))["отправлено"] == 0


async def pickup_order(db, *, arrived: datetime, storage_until=None, ozon=True) -> Order:
    async with db() as session:
        order = Order(peer_id=PEER, items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 1500}],
                      items_total=1500, delivery_cost=121, total=1621,
                      delivery_method="ozon_pvz" if ozon else "cdek_pvz", status="confirmed",
                      payment_status="succeeded", ozon_posting="0123-1" if ozon else None,
                      details={"ozon_point_address": "Краснодар, Красная, 176"} if ozon else {"cdek_number": "1"},
                      at_pickup_at=arrived, handed_over_at=arrived - timedelta(days=3), storage_until=storage_until)
        session.add(order)
        await session.commit()
        return order


async def test_pickup_reminders_second_day_and_last_day(clean, tester):
    await on(flags.PICKUP_REMINDERS)
    order = await pickup_order(clean, arrived=datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc))
    # 18.11 прибыла, хранится 7 дней — до 25.11. 19.11 — первое напоминание.
    day2 = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
    assert (await pickup_reminders.check(day2))["pickup_reminder_1"] == 1
    assert "Хранится до 25 ноября" in tester[-1] and "Красная, 176" in tester[-1]
    assert "приложении Ozon" in tester[-1]
    assert (await pickup_reminders.check(day2 + timedelta(hours=2)))["pickup_reminder_1"] == 0
    last = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    assert (await pickup_reminders.check(last))["pickup_reminder_2"] == 1
    assert tester[-1].startswith(f"Завтра последний день: посылка по заказу №{order.id}")
    assert await events(clean) == ["pickup_reminder_1", "pickup_reminder_2"]


async def test_pickup_date_from_carrier_and_quiet_hours(clean, tester):
    await on(flags.PICKUP_REMINDERS)
    await pickup_order(clean, arrived=datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc),
                       storage_until=date(2026, 11, 21), ozon=False)
    night = datetime(2026, 11, 19, 21, 0, tzinfo=timezone.utc)  # 00:00 МСК
    assert (await pickup_reminders.check(night))["skipped"] == "тихие часы"
    day = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    # Срок от СДЭКа — 21.11: 20.11 уже «завтра последний день», первое не шлём.
    assert (await pickup_reminders.check(day)) == {"pickup_reminder_1": 0, "pickup_reminder_2": 1}
    assert "SMS" in tester[-1] and "21 ноября" in tester[-1]


CATALOG = [
    {"name": "Те Гуань Инь 100 г", "price": 1500, "in_stock": True, "recommended": []},
    {"name": "Да Хун Пао 100 г", "price": 1700, "in_stock": True, "recommended": []},
    {"name": "Габа 100 г", "price": 1550, "in_stock": True, "recommended": []},
    {"name": "Шу пуэр 50 г", "price": 900, "in_stock": True, "recommended": []},
]


async def test_free_delivery_nudge_once_with_the_cheapest_closing_pack(clean, tester, monkeypatch):
    await on(flags.FREE_DELIVERY_NUDGE)
    monkeypatch.setattr(catalog_service, "load_items", lambda: CATALOG)
    monkeypatch.setattr(settings, "free_delivery_threshold", "3000")
    result = await conversation._execute_propose_order(PEER, {"items": [{"name": "Те Гуань Инь 100 г", "quantity": 1}]})
    text_ = result if isinstance(result, str) else result.tool_result
    # Не хватает 1500: закрывает самая дешёвая из пачек не дешевле 1500 — Те Гуань Инь, а не пуэр за 900.
    assert "До бесплатной доставки в пункт Ozon не хватает 1500 ₽ — разницу закроет Те Гуань Инь 100 г (1500 ₽)" in text_
    assert "До бесплатной доставки не хватает" not in text_
    assert "free_delivery_nudge_shown" in await events(clean)
    await conversation._execute_add_to_order(PEER, {"items": [{"name": "Те Гуань Инь 100 г", "quantity": 1}]})
    assert "free_delivery_nudge_accepted" in await events(clean)
    again = await conversation._execute_propose_order(PEER, {"items": [{"name": "Шу пуэр 50 г", "quantity": 1}]})
    assert "в пункт Ozon" not in (again if isinstance(again, str) else again.tool_result)
