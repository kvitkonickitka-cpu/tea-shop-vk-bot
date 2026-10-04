"""Задача 5: на названной улице один пункт — записать его и сразу счёт."""

from __future__ import annotations

from types import SimpleNamespace as NS

from sqlalchemy import select

from app.messages import keyboard as keyboards
from app.messages.models import FunnelEvent
from app.modules.delivery import ozon_quote
from app.modules.dialog import service as dialog_service
from app.modules.orders import conversation, state
from app.modules.orders.state import OrderDraft
from tests.test_storefront_vk_flow import ADMIN, shop  # noqa: F401 — фикстура
from tests.test_vk_buttons import FULL, KRD, PEER, say, world  # noqa: F401

BLAGOEVA = NS(id=31, address="Россия, Краснодарский край, Краснодар, улица Благоева, 2/1")


def two_tools(*blocks):
    return NS(stop_reason="tool_use", content=[
        NS(type="tool_use", id=f"t-{i}", name=name, input=data) for i, (name, data) in enumerate(blocks)
    ])


async def one_on_blagoeva(draft, city, hint=""):
    if "благоев" in hint.lower():
        return ozon_quote.Picked([BLAGOEVA], 1, 1, True)
    return ozon_quote.Picked(KRD, len(KRD), len(KRD), not hint)


async def events(db):
    async with db() as session:
        return [row.event for row in (await session.execute(select(FunnelEvent))).scalars()]


async def test_storefront_reply_with_street_and_email_gets_the_invoice(clean, shop, monkeypatch):
    from app.modules.catalog import service as catalog_service

    monkeypatch.setattr(conversation, "_ozon_points", one_on_blagoeva)
    monkeypatch.setattr(catalog_service, "load_items", lambda: [
        {"name": "Дянь Хун 100 г", "price": 1500, "in_stock": True, "recommended": []}])
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    # Клиент — одним сообщением: улица и почта. Модель зовёт оба инструмента.
    shop["script"] = [two_tools(
        ("set_delivery_method", {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Благоева"}),
        ("set_recipient", {"name": "Nikita Kvitko", "phone": "+79214477622", "email": "petrov@mail.ru"}),
    )]
    await say("Благоева, petrov@mail.ru", 2)
    text, board = shop["sent"][-1]
    assert text.startswith("Заказ №")
    assert ("Доставка: пункт выдачи Ozon, Россия, Краснодарский край, Краснодар, улица Благоева, 2/1 — 121 ₽\n"
            "Если пункт не тот — напишите, поменяю.\nСрок: ≈ 11 октября") in text
    assert len(shop["payments"]) == 1
    assert "invoice_single_point" in await events(clean)


async def test_without_data_one_message_asks_for_them(clean, world, monkeypatch):
    monkeypatch.setattr(conversation, "_ozon_points", one_on_blagoeva)
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery", details={}))
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Благоева"})
    assert ("«На улице Благоева один пункт Ozon: ул. Благоева, 2/1 — 121 ₽, получите ≈ 11 октября. "
            "Пришлите почту (или ФИО, телефон и почту) — сразу пришлю счёт на этот пункт. "
            "Нужен другой — напишите улицу.»") in result.tool_result
    draft = await state.get_draft(PEER)
    assert draft.details["ozon_point_id"] == 31 and draft.details["single_point"]
    # Назвал другую улицу — пункт ищется заново, отметка снимается.
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"})
    draft = await state.get_draft(PEER)
    assert "single_point" not in draft.details and "ozon_point_id" not in draft.details


async def test_two_points_on_the_street_still_ask(clean, world, monkeypatch):
    async def two(draft, city, hint=""):
        return ozon_quote.Picked(KRD[:2], 2, 2, True)

    monkeypatch.setattr(conversation, "_ozon_points", two)
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery", details={}))
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"})
    assert "один пункт" not in result.tool_result
    assert "ozon_point_id" not in (await state.get_draft(PEER)).details
