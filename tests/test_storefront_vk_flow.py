"""Заказ из «Товаров»: служебные сообщения ВК, выбор Ozon или СДЭК кнопкой, шаблонный вопрос."""

from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.messages import keyboard as keyboards, manager as manager_messages, templates
from app.modules.delivery import cdek_client
from app.modules.dialog import history as dialog_history, service as dialog_service
from app.modules.orders import conversation, service as orders_service, state, vk_orders_client
from tests.test_auto_invoice import said
from tests.test_vk_buttons import FULL, PEER, say, world  # noqa: F401

ADMIN = (
    "Новый заказ 820826\nСтоимость заказа: 1 500 руб.\n\nКорзина:\nДянь Хун // 100 грамм (1) 1500 ₽\n\n"
    "Способ доставки: по умолчанию\nАдрес доставки: Краснодар\nПолучатель: Nikita Kvitko\n"
    "Контактный телефон: +79214477622\n\n"
    "Управление заказами: https://vk.ru/teapotwice?act=market_group_orders&source=im_reminder\n"
    f"Связаться с покупателем: https://vk.ru/gim240363526?sel={PEER}\n"
    "Вы можете отключить получение этих уведомлений в настройках: https://vk.ru/teapotwice?act=market_group_settings"
)
CLIENT = (
    "Ваш заказ № 820826 оформлен\n\nДоставка в пункт выдачи СДЭК: Краснодар\n\n"
    f"Ожидаемая дата доставки: 5 октября\n\nПодробнее о заказе: https://vk.ru/orders{PEER}_820826\n\n"
    "К оплате: 1 500 руб."
)
CLIENT_ATTACHMENTS = [{"type": "market", "market": {"title": "Дянь Хун // 100 грамм", "price": {"amount": "150000"}}}]
QUESTION = "Здравствуйте!\nПодскажите, пожалуйста, как оплатить заказ и когда сможете доставить?"


@pytest.fixture
def shop(world, monkeypatch):
    world["manager"] = []

    async def to_manager(text, chat_id=None):
        world["manager"].append(text)

    async def cdek(draft, method, address, delivery_point=None):
        return cdek_client.Tariff(136, "Посылка склад-склад", 200.0, 2, 3, 4), 245.0

    async def no_order(order_id, user_id=None):
        raise RuntimeError("VK API error")

    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    monkeypatch.setattr(conversation, "_cdek_delivery", cdek)
    monkeypatch.setattr(vk_orders_client, "get_order_items", no_order)
    return world


def labels(board):
    return [b["action"]["label"] for row in (board or {"buttons": []})["buttons"] for b in row]


def test_both_notices_are_parsed():
    admin = orders_service.parse_notice(ADMIN)
    assert (admin.order_id, admin.kind, admin.user_id, admin.address) == (820826, "admin", PEER, "Краснодар")
    assert admin.items == [{"name": "Дянь Хун // 100 грамм", "quantity": 1, "price": 1500.0}]
    assert admin.recipient == ("Nikita Kvitko", "+79214477622")
    client = orders_service.parse_notice(CLIENT, CLIENT_ATTACHMENTS)
    assert (client.order_id, client.kind, client.user_id, client.address) == (820826, "client", PEER, "Краснодар")
    assert client.items == [{"name": "Дянь Хун // 100 грамм", "quantity": 1, "price": 1500.0}]
    assert orders_service.parse_notice("Заказ №12 — проверьте, всё ли верно") is None


async def test_order_from_the_admin_notice_offers_two_carriers_once(clean, shop, monkeypatch):
    # Прежний путь — две кнопки перевозчиков: при выключенном флаге задачи 3.
    monkeypatch.setattr(settings, "storefront_direct_ozon_enabled", False)
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    text, board = shop["sent"][-1]
    assert text.startswith("Заказ №820826 принят: Дянь Хун // 100 грамм × 1 — 1500 ₽.\n\nДоставка в Краснодар:\n")
    assert "• Ozon, пункт выдачи — 121 ₽, получите ≈ 11 октября" in text
    assert "• СДЭК, пункт выдачи — 245 ₽, получите ≈ 7–8 октября" in text
    assert "пришлите почту для чека" in text and text.endswith("Выберите доставку кнопкой ниже 👇")
    assert labels(board) == ["Ozon — 121 ₽", "СДЭК — 245 ₽"]
    # Модель видит заказ в истории — без телефона и почты.
    notes = [m["content"] for m in await dialog_history.get_history(PEER) if "Товары" in m["content"]]
    assert notes == ["[уведомление ВК] Клиент оформил заказ №820826 в разделе «Товары» сообщества: "
                     "Дянь Хун // 100 грамм × 1 — 1500 ₽, доставка: Краснодар."]
    # Сообщение клиенту и событие ВК о том же заказе — второго предложения нет.
    sent = len(shop["sent"])
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": CLIENT, "attachments": CLIENT_ATTACHMENTS})
    await orders_service.handle_new_order({"id": 820826, "user_id": PEER})
    assert len(shop["sent"]) == sent
    assert any("№820826" in card for card in shop["manager"])


async def test_choosing_a_carrier_asks_point_and_email(clean, shop, monkeypatch):
    monkeypatch.setattr(settings, "storefront_direct_ozon_enabled", False)
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    ozon = shop["sent"][-1][1]["buttons"][0][0]["action"]
    await say(ozon["label"], 2, json.loads(ozon["payload"]))
    text, _ = shop["sent"][-1]
    assert text.startswith("Ozon, пункт выдачи — 121 ₽")
    assert templates.ask_point_address("Ozon") in text  # город большой — просим адрес пункта
    assert "Получатель из заказа: Nikita Kvitko, +79214477622. Пришлите, пожалуйста, почту — на неё придёт чек" in text
    draft = await state.get_draft(PEER)
    assert draft.delivery_method == "ozon_pvz" and shop["model"] == []


async def test_template_question_waits_for_the_order_and_stays_silent(clean, shop, monkeypatch):
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    sent = len(shop["sent"])
    await say(QUESTION, 1)
    # Ответ на вопрос — уже отправленное предложение доставки: модель не зовём.
    assert shop["model"] == [] and len(shop["sent"]) == sent
    assert (await dialog_history.get_history(PEER))[-1]["content"] == QUESTION


async def test_template_question_without_an_order_tells_the_model(clean, shop, monkeypatch):
    prompts = []

    async def converse(messages, system_prompt, tools):
        prompts.append(system_prompt)
        return said("Вижу ваш заказ, сейчас пришлю варианты доставки.")

    async def no_wait(peer_id, seconds=15):
        return None

    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    monkeypatch.setattr(orders_service, "wait_for_order", no_wait)
    await say(QUESTION, 1)
    assert conversation._STOREFRONT_PENDING in prompts[0]
    assert shop["sent"][-1][0] == "Вижу ваш заказ, сейчас пришлю варианты доставки."


async def test_get_order_unwraps_the_order(monkeypatch):
    class Response:
        def json(self):
            return {"response": {"order": {"id": 820826, "user_id": PEER}}}

    class Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params):
            assert params["user_id"] == PEER
            return Response()

    monkeypatch.setattr(vk_orders_client.httpx, "AsyncClient", Client)
    assert await vk_orders_client.get_order(820826, user_id=PEER) == {"id": 820826, "user_id": PEER}
