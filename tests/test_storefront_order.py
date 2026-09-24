"""Заказ из витрины ВК становится обычным черновиком.

Проверка жила во временных скриптах; переношу в набор, потому что ветку
задели переводом уведомлений менеджеру на очередь.
"""

from __future__ import annotations

import pytest

from app.messages import manager as manager_messages
from app.modules.orders import service as orders_service, state, vk_orders_client

USER = 8500


@pytest.fixture
def channels(monkeypatch):
    box: dict = {"client": [], "manager": []}

    async def to_client(peer_id, text, random_id=None):
        box["client"].append(text)

    async def to_manager(text, chat_id=None):
        box["manager"].append(text)

    async def get_order(order_id):
        return {
            "id": order_id, "user_id": USER,
            "total_price": {"amount": 160000},
            "delivery_address": "Краснодар, ул. Ставропольская, 230",
        }

    monkeypatch.setattr(orders_service.vk_client, "send_message", to_client)
    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    monkeypatch.setattr(vk_orders_client, "get_order", get_order)
    return box


async def test_storefront_order_becomes_a_draft(clean, channels, monkeypatch):
    async def get_items(order_id):
        return [
            {"item": {"title": "Те Гуань Инь", "price": {"amount": "80000"}},
             "quantity": 2, "price": {"amount": "160000"}},
        ]

    monkeypatch.setattr(vk_orders_client, "get_order_items", get_items)
    await state.clear_draft(USER)

    await orders_service.handle_new_order({"id": 41})

    draft = await state.get_draft(USER)
    assert draft is not None and draft.stage == "awaiting_delivery"
    assert draft.items == [{"name": "Те Гуань Инь", "quantity": 2, "price": 800.0}]
    assert draft.items_total == 1600.0
    assert draft.details["vk_order_id"] == 41
    assert "Ставропольская" in draft.details["vk_order_address"]

    # Клиента ведём в обычный флоу: Ozon первым, спрашиваем город.
    assert "Ozon" in channels["client"][-1] and "город" in channels["client"][-1]
    # И никакой собственной цены СДЭКа в этой ветке больше нет.
    assert "руб, срок" not in channels["client"][-1]
    assert channels["manager"], "менеджер не узнал про витринный заказ"


async def test_unparsed_items_do_not_lose_the_order(clean, channels, monkeypatch):
    async def get_items_broken(order_id):
        return [{"nope": True}]

    monkeypatch.setattr(vk_orders_client, "get_order_items", get_items_broken)
    await state.clear_draft(USER)

    await orders_service.handle_new_order({"id": 42})

    assert await state.get_draft(USER) is None
    assert "принят" in channels["client"][-1]
    assert "руками" in channels["manager"][-1]


async def test_unit_price_falls_back_to_the_line_total(clean, channels, monkeypatch):
    async def get_items(order_id):
        return [{"item": {"title": "Да Хун Пао"}, "quantity": 2, "price": {"amount": "220000"}}]

    monkeypatch.setattr(vk_orders_client, "get_order_items", get_items)
    await state.clear_draft(USER)

    await orders_service.handle_new_order({"id": 43})

    draft = await state.get_draft(USER)
    assert draft.items[0]["price"] == 1100.0
