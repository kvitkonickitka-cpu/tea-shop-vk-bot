"""Кнопка «Да, на эти данные» под вопросом о прошлом получателе."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from app.modules.orders import conversation, repeat_delivery, repository as orders_repository, state
from app.modules.orders.models import Order
from app.modules.orders.state import OrderDraft
from tests.test_auto_invoice import said
from tests.test_pickup_choice import KRD
from tests.test_vk_buttons import PEER, say, world  # noqa: F401


async def old_order(db):
    # Телефон в старом заказе — как его когда-то написали.
    async with db() as session:
        session.add(Order(
            peer_id=PEER, items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}],
            items_total=1500, delivery_cost=117, total=1617, delivery_method="ozon_pvz",
            status="confirmed", payment_status=orders_repository.PAID,
            details={"address": "Краснодар", "recipient_name": "Квитко Никита Александрович",
                     "recipient_phone": "89214477622", "recipient_email": "k@gmail.com"},
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        ))
        await session.commit()


async def test_last_recipient_phone_is_shown_normalized(clean):
    await old_order(clean)
    last = await repeat_delivery.last_recipient_for(PEER)
    assert last.phone == "+79214477622"
    assert "+79214477622" in repeat_delivery.recipient_suggestion(last)
    assert "89214477622" not in repeat_delivery.recipient_suggestion(last)


async def chosen_point_draft():
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        delivery_method="ozon_pvz", delivery_label=f"Ozon, пункт выдачи: {KRD[0].address}",
        delivery_cost=121, stage="awaiting_confirmation",
        details={"address": "Краснодар", "ozon_point_id": 11, "ozon_point_address": KRD[0].address,
                 "quoted_at": time.time(), "seen_total": 1621},
    ))


async def test_button_records_recipient_and_sends_invoice(clean, world):
    await old_order(clean)
    await chosen_point_draft()
    world["script"] = [said("Оформить на получателя, как в прошлый раз — Квитко Никита Александрович, +79214477622, k@gmail.com?")]
    await say("те гуань есть?", 1)
    text, board = world["sent"][-1]
    button = board["buttons"][0][0]["action"]
    assert button["label"] == "Да, на эти данные"

    await say(button["label"], 2, json.loads(button["payload"]))
    text, board = world["sent"][-1]
    assert world["payments"] == [1] and world["model"] == ["те гуань есть?"]
    assert "Получатель: Квитко Никита Александрович, +79214477622, k@gmail.com" in text
    assert board["buttons"][0][0]["action"]["label"] == "Оплатить 1621 ₽"


async def test_button_next_to_points_asks_only_for_point(clean, world):
    await old_order(clean)
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery"))
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"})
    world["script"] = [said("Пункты: 1) Ставропольская, 230 2) … Оформить на прошлого получателя?")]
    await say("какие пункты?", 1)
    rows = world["sent"][-1][1]["buttons"]
    assert rows[-1][0]["action"]["label"] == "Да, на эти данные" and len(rows) == 5  # 4 пункта + получатель

    await say("Да, на эти данные", 2, json.loads(rows[-1][0]["action"]["payload"]))
    assert world["sent"][-1][0] == (
        "Записала получателя: Квитко Никита Александрович, +79214477622, k@gmail.com.\n"
        "Выберите пункт выдачи — сразу пришлю счёт."
    )
    point = world["sent"][-1][1]["buttons"][0][0]["action"]
    await say(point["label"], 3, json.loads(point["payload"]))
    assert world["payments"] == [1] and "Итого к оплате с учётом доставки: 1621 ₽" in world["sent"][-1][0]


async def test_no_button_once_recipient_is_written(clean, world):
    await old_order(clean)
    await chosen_point_draft()
    draft = await state.get_draft(PEER)
    draft.details.update(recipient_name="Иванов Иван", recipient_phone="+79001234567")
    await state.set_draft(PEER, draft)
    world["script"] = [said("Нужна почта для чека.")]
    await say("что дальше?", 1)
    assert world["sent"][-1][1] is None
