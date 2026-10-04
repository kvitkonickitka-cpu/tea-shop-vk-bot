"""Задача 3: заказ из «Товаров» сразу по самому дешёвому варианту — без шага выбора перевозчика."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.core.config import settings
from app.messages import keyboard as keyboards
from app.messages.models import FunnelEvent
from app.modules.dialog import service as dialog_service
from app.modules.orders import service as orders_service, state
from tests.test_storefront_vk_flow import ADMIN, CLIENT, labels, shop  # noqa: F401 — фикстура
from tests.test_vk_buttons import FULL, PEER, say, world  # noqa: F401


async def journal(db):
    async with db() as session:
        rows = (await session.execute(select(FunnelEvent).order_by(FunnelEvent.id))).scalars().all()
    return [(row.event, row.data) for row in rows]


async def test_first_message_leads_with_ozon_and_asks_place_and_email(clean, shop):
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    text, board = shop["sent"][-1]
    assert text.splitlines()[:2] == [
        "Заказ №820826 принят: Дянь Хун // 100 грамм × 1 — 1500 ₽.",
        "Дешевле всего — пункт выдачи Ozon: доставка 121 ₽, получите ≈ 11 октября. "
        "Итого 1621 ₽ — к сумме из «Товаров» прибавляется доставка.",
    ]
    # Город большой, улицы в заказе нет — спрашиваем, где удобно забрать.
    assert "Где удобно забрать? Напишите улицу — покажу ближайшие пункты." in text
    assert ("Получатель из заказа: Nikita Kvitko, +79214477622. Пришлите почту для чека — и сразу "
            "пришлю ссылку на оплату.") in text
    assert text.splitlines()[-1] == "Нужно быстрее — СДЭК: 245 ₽, получите ≈ 7–8 октября."
    assert labels(board) == ["Нужно быстрее — СДЭК"]
    draft = await state.get_draft(PEER)
    assert draft.delivery_method == "ozon_pvz" and draft.details["point_asked"]
    # Способ доставки из «Товаров» — в журнале, кодом.
    assert ("storefront_vk_delivery", {"vk_order_id": 820826, "kind": "default"}) in await journal(clean)


async def krd_cdek(city):
    from app.modules.delivery import cdek_client

    return [cdek_client.DeliveryPoint(code="KRD1", address="ул. Красная, 1", work_time="")]


async def test_faster_button_goes_to_the_cdek_branch(clean, shop, monkeypatch):
    from app.modules.orders import conversation

    monkeypatch.setattr(conversation.cdek_client, "city_points", krd_cdek)
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    faster = shop["sent"][-1][1]["buttons"][-1][0]["action"]
    await say(faster["label"], 2, json.loads(faster["payload"]))
    text, _ = shop["sent"][-1]
    assert text.startswith("СДЭК, пункт выдачи — 245 ₽")
    assert (await state.get_draft(PEER)).delivery_method == "cdek_pvz" and shop["model"] == []


async def test_above_threshold_faster_is_a_surcharge(clean, shop):
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN.replace(
        "Дянь Хун // 100 грамм (1) 1500 ₽", "Дянь Хун // 100 грамм (2) 3000 ₽")})
    text, _ = shop["sent"][-1]
    assert "Дешевле всего — пункт выдачи Ozon: доставка бесплатно, получите ≈ 11 октября." in text
    assert "прибавляется доставка" not in text
    assert text.splitlines()[-1] == "Нужно быстрее — СДЭК: с доплатой 124 ₽, получите ≈ 7–8 октября."


async def test_client_notice_reads_the_vk_delivery_and_can_lead_with_it(clean, shop, monkeypatch):
    monkeypatch.setattr(settings, "storefront_respect_vk_delivery", True)
    await keyboards.remember_client(PEER, FULL)
    from tests.test_storefront_vk_flow import CLIENT_ATTACHMENTS

    assert orders_service.parse_notice(CLIENT, CLIENT_ATTACHMENTS).delivery == "cdek"
    assert orders_service.parse_notice(ADMIN).delivery == "default"

    from app.modules.orders import conversation

    monkeypatch.setattr(conversation.cdek_client, "city_points", krd_cdek)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": CLIENT, "attachments": CLIENT_ATTACHMENTS})
    text, board = shop["sent"][-1]
    assert "Вы выбрали СДЭК — пункт выдачи СДЭК: доставка 245 ₽, получите ≈ 7–8 октября." in text
    assert "Пункты выдачи СДЭК рядом:\n1) ул. Красная, 1" in text
    assert text.splitlines()[-2] == "Дешевле на 124 ₽ — Ozon: 121 ₽, получите ≈ 11 октября."
    assert labels(board)[-1] == "Дешевле — Ozon"
    # Без получателя в заказе — просим всё сразу.
    assert "Пришлите ФИО, телефон и почту получателя — и сразу пришлю ссылку на оплату." in text


async def test_flag_off_keeps_two_carrier_buttons(clean, shop, monkeypatch):
    monkeypatch.setattr(settings, "storefront_direct_ozon_enabled", False)
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    assert labels(shop["sent"][-1][1]) == ["Ozon — 121 ₽", "СДЭК — 245 ₽"]


async def test_lead_asks_email_for_the_form_recipient_not_the_past_one(clean, shop, monkeypatch):
    from app.modules.orders import repeat_delivery

    async def past(peer_id):
        return repeat_delivery.LastRecipient(1, "Квитко Никита Александрович", "+79990001122", "k@yandex.ru")

    monkeypatch.setattr(repeat_delivery, "last_recipient_for", past)
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    text, board = shop["sent"][-1]
    assert "Получатель из заказа: Nikita Kvitko, +79214477622. Пришлите почту для чека" in text
    assert "Да, на эти данные" not in labels(board)


async def test_canceling_a_storefront_order_names_it_and_tells_the_manager(clean, shop):
    from tests.test_auto_invoice import tool_use

    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    shop["script"] = [tool_use("cancel_order")]
    await say("отмените заказ", 2)
    assert shop["sent"][-1][0].startswith("Отменила заказ №820826 — оплачивать его не нужно.")
    assert any("Отмените его в разделе «Заказы»" in card and "№820826" in card for card in shop["manager"])
