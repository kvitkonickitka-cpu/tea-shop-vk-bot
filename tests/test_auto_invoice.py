"""Задача 2: сводка и ссылка одним сообщением, правка после ссылки, снимок счёта."""

from __future__ import annotations

import time
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.messages.models import FunnelEvent
from app.modules.catalog import service as catalog_service
from app.modules.orders import conversation, repository as orders_repository, shipping, state
from app.modules.orders.models import Order, OrderPayment
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service, webhook, yookassa_client

PEER = 9950
CATALOG = [
    {"name": "Те Гуань Инь (тест)", "price": 1500, "in_stock": True, "recommended": []},
    {"name": "Да Хун Пао", "price": 1500, "in_stock": True, "recommended": []},
]
RECIPIENT = {"name": "Иванов Иван", "phone": "89001234567", "email": "ivanov@mail.ru"}


def tool_use(tool: str, **tool_input):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=f"t-{tool}", name=tool, input=tool_input)])


def said(text: str):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)])


@pytest.fixture
def world(monkeypatch):
    box = {"payments": [], "model": [], "catalog": [dict(i) for i in CATALOG], "quotes": 0}

    async def create_payment(draft, order_key, attempt=1):
        n = len(box["payments"]) + 1
        box["payments"].append({"attempt": attempt, "items": [dict(i) for i in draft.items],
                                "email": draft.details.get("recipient_email")})
        return yookassa_client.Payment(
            id=f"pay-{n}", status="pending", paid=False,
            confirmation_url=f"https://yoomoney.ru/checkout/{n}", receipt_registration="",
            test=False, amount=draft.items_total + (draft.delivery_cost or 0),
        )

    async def converse(messages, system_prompt, tools):
        box["model"].append({"prompt": system_prompt, "tools": [t["name"] for t in tools]})
        return box["script"].pop(0)

    async def ozon_price(draft, point_id):
        box["quotes"] += 1
        return NS(total=121.0, days=6)

    async def to_client(peer_id, text, random_id=None):
        pass

    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    monkeypatch.setattr(payment_service, "create_payment", create_payment)
    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    monkeypatch.setattr(conversation, "_ozon_price", ozon_price)
    monkeypatch.setattr(catalog_service, "load_items", lambda: box["catalog"])
    monkeypatch.setattr(settings, "free_delivery_threshold", "3000")
    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    return box


async def point_chosen(**details) -> OrderDraft:
    draft = OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        delivery_method="ozon_pvz", delivery_label="Ozon, пункт выдачи: Краснодар, Ставропольская, 230",
        delivery_cost=121, stage="awaiting_confirmation",
        details={"address": "Краснодар", "ozon_point_id": 7, "ozon_point_address": "Краснодар, Ставропольская, 230",
                 "quoted_at": time.time(), "seen_total": 1621, **details},
    )
    await state.set_draft(PEER, draft)
    return draft


async def test_recipient_completes_order_and_invoice_goes_at_once(clean, world):
    await point_chosen()
    world["script"] = [tool_use("set_recipient", **RECIPIENT)]
    reply = await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, ivanov@mail.ru")

    assert len(world["payments"]) == 1 and len(world["model"]) == 1
    async with clean() as session:
        order = (await session.execute(select(Order))).scalar_one()
        attempt = (await session.execute(select(OrderPayment))).scalar_one()
        events = (await session.execute(select(FunnelEvent.event))).scalars().all()
    assert reply == (
        f"Заказ №{order.id} — проверьте, всё ли верно:\n"
        "• Те Гуань Инь (тест) × 1 — 1500 ₽\n"
        "Доставка: пункт выдачи Ozon, Краснодар, Ставропольская, 230 — 121 ₽\n"
        "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru\n"
        "Итого: 1621 ₽\n\n"
        "Оплатить: https://yoomoney.ru/checkout/1\n"
        "Ссылка действует 60 минут. После оплаты пришлём чек на ivanov@mail.ru и сразу "
        "передадим заказ в доставку.\n"
        "Условия покупки, доставки и возврата: https://vk.ru/@teapotwice-usloviya-dostavki-oplaty-i-vozvrata\n"
        "Если что-то не так — напишите, поправлю и пришлю новую ссылку."
    )
    assert attempt.snapshot["items"][0]["name"] == "Те Гуань Инь (тест)"
    assert attempt.snapshot["details"]["recipient_email"] == "ivanov@mail.ru"
    assert events == ["invoice_auto"]
    assert await state.get_draft(PEER) is None


async def test_no_invoice_while_email_typo_is_pending(clean, world, monkeypatch):
    await point_chosen()
    world["script"] = [tool_use("set_recipient", name="Иванов Иван", phone="89001234567",
                                email="ivanov@yandex.ry"),
                       said("Может, ivanov@yandex.ru?")]
    reply = await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, ivanov@yandex.ry")
    assert reply == "Может, ivanov@yandex.ru?" and world["payments"] == []
    assert (await state.get_draft(PEER)).details["email_suggestion"] == "ivanov@yandex.ru"


async def test_changed_price_asks_again(clean, world):
    await point_chosen()
    world["catalog"][0]["price"] = 1700
    world["script"] = [tool_use("set_recipient", **RECIPIENT), said("Цена изменилась… Оформляем?")]
    await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, ivanov@mail.ru")
    assert world["payments"] == []
    # Модель получила объяснение вместе с результатом set_recipient.
    draft = await state.get_draft(PEER)
    assert draft.items[0]["price"] == 1700 and draft.details["seen_total"] == 1821
    # На «да» — confirm_order, как раньше.
    result = await conversation._execute_confirm_order(PEER)
    assert "Итого: 1821 ₽" in result.client_reply and len(world["payments"]) == 1


async def test_out_of_stock_blocks_invoice(clean, world):
    await point_chosen()
    world["catalog"][0]["in_stock"] = False
    note = await conversation._refresh_before_invoice(PEER, await state.get_draft(PEER))
    assert "нет в наличии" in note
    world["script"] = [tool_use("set_recipient", **RECIPIENT), said("Этого чая сейчас нет…")]
    await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, ivanov@mail.ru")
    assert world["payments"] == []


async def test_old_quote_is_recalculated(clean, world):
    await point_chosen(quoted_at=time.time() - 31 * 60)
    world["script"] = [tool_use("set_recipient", **RECIPIENT)]
    await conversation.handle_turn(PEER, "данные")
    assert world["quotes"] == 1 and len(world["payments"]) == 1


async def test_edit_after_link_reissues_same_order(clean, world):
    await point_chosen()
    world["script"] = [tool_use("set_recipient", **RECIPIENT)]
    await conversation.handle_turn(PEER, "данные")

    # Ссылка выставлена, черновика нет — клиент меняет получателя.
    world["script"] = [tool_use("set_recipient", name="Петров Пётр", phone="89007654321",
                                email="petrov@mail.ru")]
    reply = await conversation.handle_turn(PEER, "получатель другой: Петров Пётр, 89007654321, petrov@mail.ru")
    assert "set_recipient" in world["model"][-1]["tools"]
    assert "ссылка на оплату уже выставлена" in world["model"][-1]["prompt"]

    async with clean() as session:
        orders = (await session.execute(select(Order))).scalars().all()
        attempts = (await session.execute(select(OrderPayment).order_by(OrderPayment.attempt))).scalars().all()
    assert len(orders) == 1 and "Получатель: Петров Пётр" in reply
    assert f"Заказ №{orders[0].id}" in reply
    assert [a.attempt for a in attempts] == [1, 2]
    assert attempts[0].closed_at is not None and attempts[1].closed_at is None
    assert orders[0].payment_id == "pay-2"


async def test_paying_old_link_ships_what_was_paid(clean, world, monkeypatch):
    await point_chosen()
    world["script"] = [tool_use("set_recipient", **RECIPIENT)]
    await conversation.handle_turn(PEER, "данные")
    world["script"] = [tool_use("add_to_order", items=[{"name": "Да Хун Пао", "quantity": 1}]),
                       tool_use("set_delivery_method", method="ozon_pvz", address="Краснодар",
                                pickup_point="Краснодар, Ставропольская, 230")]

    async def points(draft, city, hint=""):
        from app.modules.delivery import ozon_quote
        return ozon_quote.Picked([NS(id=7, address="Краснодар, Ставропольская, 230")], 1, 1, True)

    monkeypatch.setattr(conversation, "_ozon_points", points)
    monkeypatch.setattr(conversation.ozon_quote, "is_ready", lambda: True)
    reply = await conversation.handle_turn(PEER, "добавьте Да Хун Пао")
    assert "• Да Хун Пао × 1" in reply and "Доставка: пункт выдачи Ozon, Краснодар, Ставропольская, 230 — бесплатно" in reply

    shipped, manager = [], []

    async def register(**kwargs):
        shipped.append(kwargs)
        return shipping.Registered(ozon_posting="0001-1")

    async def to_manager(order, text):
        manager.append(text)
        return True

    monkeypatch.setattr(webhook.shipping, "register", register)
    monkeypatch.setattr(webhook.order_chat, "send", to_manager)
    monkeypatch.setattr(yookassa_client, "cancel_payment", lambda pid: _raise())

    await webhook.handle_paid(yookassa_client.Payment(
        id="pay-1", status="succeeded", paid=True, confirmation_url="",
        receipt_registration="pending", test=False, amount=1621.0,
    ))
    assert [i["name"] for i in shipped[0]["items"]] == ["Те Гуань Инь (тест)"]
    assert any("оплачен прошлый вариант заказа" in text for text in manager)
    async with clean() as session:
        order = (await session.execute(select(Order))).scalar_one()
    assert float(order.total) == 1621 and len(order.items) == 1


async def _raise():
    raise yookassa_client.YooKassaError("payment can not be canceled")


def test_prompt_has_no_mandatory_confirmation():
    prompt = conversation.order_flow_prompt()
    assert "Не спрашивай «Всё верно, оформляем?»" in prompt
    assert "Всё верно, оформляем?»\n\n8. Если клиент подтвердил" not in prompt


async def test_flag_turns_auto_invoice_off(clean, world, monkeypatch):
    monkeypatch.setattr(settings, "auto_invoice_enabled", False)
    await point_chosen()
    world["script"] = [tool_use("set_recipient", **RECIPIENT), said("Всё верно, оформляем?")]
    await conversation.handle_turn(PEER, "данные")
    assert world["payments"] == []
