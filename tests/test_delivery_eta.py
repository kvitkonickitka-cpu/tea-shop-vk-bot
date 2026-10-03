"""Срок доставки для клиента: сборка у нас плюс путь у перевозчика."""

from __future__ import annotations

import pytest

from app.messages import templates
from app.modules.delivery import cdek_client
from app.modules.orders import conversation, eta, offers, state
from app.modules.orders.repeat_delivery import LastDelivery, LastRecipient
from app.modules.orders.state import OrderDraft
from tests.test_pickup_choice import PEER, fresh_draft, ozon  # noqa: F401 — фикстура


def _eta(carrier, low, high, working):
    details: dict = {}
    eta.remember(details, carrier=carrier, days_min=low, days_max=high, working=working)
    return details


def test_phrase_ozon_and_cdek():
    assert eta.phrase(_eta("ozon", 5, 5, False)) == "≈ 6 дней: 1 день соберём и сдадим, 5 дней в пути у Ozon"
    assert eta.phrase(_eta("cdek", 3, 4, True)) == (
        "≈ 4–5 рабочих дней: 1 день соберём и сдадим, 3–4 рабочих дня в пути у СДЭКа"
    )
    assert eta.phrase(_eta("cdek", 1, 1, True)) == (
        "≈ 2 рабочих дня: 1 день соберём и сдадим, 1 рабочий день в пути у СДЭКа"
    )


def test_no_carrier_days_no_phrase():
    details = _eta("ozon", 5, 5, False)
    eta.remember(details, carrier="ozon", days_min=0, days_max=0, working=False)
    assert eta.phrase(details) == "" and eta.KEY not in details
    assert eta.phrase({}) == "" and eta.phrase_for(None) == ""


async def test_ozon_tool_result_and_draft_carry_the_phrase(clean, ozon):  # noqa: F811
    await fresh_draft()
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар"}
    )
    phrase = "≈ 6 дней: 1 день соберём и сдадим, 5 дней в пути у Ozon"
    assert f"срок {phrase}" in result.tool_result
    assert "Срок называй вместе с ценой" in result.tool_result
    draft = await state.get_draft(PEER)
    # Ответ инструмента в историю не попадает — срок обязан жить в черновике.
    assert f"Срок доставки (называй только так): {phrase}" in conversation._describe_draft(draft)


async def test_cdek_courier_tool_result(clean, monkeypatch):
    async def cdek(draft, method, address, delivery_point=None):
        return cdek_client.Tariff(137, "Посылка склад-дверь", 320.0, 3, 4, 3), 397.72

    monkeypatch.setattr(conversation, "_cdek_delivery", cdek)
    await fresh_draft()
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "cdek_courier", "address": "Москва, Тверская 1"}
    )
    assert "срок ≈ 4–5 рабочих дней: 1 день соберём и сдадим, 3–4 рабочих дня в пути у СДЭКа" in result.tool_result


def test_client_messages_show_the_phrase():
    phrase = "≈ 6 дней: 1 день соберём и сдадим, 5 дней в пути у Ozon"
    common = dict(
        items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 1500}],
        delivery_method="ozon_pvz", delivery_label="Ozon, пункт выдачи: Красная, 176",
        delivery_cost=117, name="Иванов Иван", phone="+79001234567", email="a@b.ru", total=1617,
    )
    invoice = templates.invoice_summary(order_id=12, link="https://pay", eta=phrase, **common)
    assert f"Доставка: пункт выдачи Ozon, Красная, 176 — 117 ₽\nСрок: {phrase}\n" in invoice
    offer = templates.returning_offer(eta=phrase, **common)
    assert f"\nСрок: {phrase}\n" in offer
    chosen = templates.point_chosen(address="Красная, 176", delivery_cost=117, total=1617,
                                    ask_recipient=False, eta=phrase)
    assert chosen.endswith(f"Срок: {phrase}.")
    # Срока нет — строки нет, а не «Срок: ».
    assert "Срок" not in templates.invoice_summary(order_id=12, link="https://pay", **common)


async def test_returning_offer_brings_the_eta_into_the_order(clean, ozon):  # noqa: F811
    draft = OrderDraft(items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}],
                       items_total=1500, stage="awaiting_delivery", details={})
    delivery = LastDelivery(1, "ozon_pvz", "Краснодар", "Красная улица, 176", point_id=13)
    recipient = LastRecipient(1, "Иванов Иван", "+79001234567", "")
    offer = await offers.prepare(draft, delivery, recipient)
    assert offer.eta == {"carrier": "ozon", "min": 5, "max": 5, "working": False}
    assert "Срок: ≈ 6 дней:" in conversation._offer_message(draft, offer)
    offers.apply(draft, offer)
    assert eta.phrase(draft.details).startswith("≈ 6 дней:")


async def test_handover_range_still_supported(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "handover_days_min", 1)
    monkeypatch.setattr(settings, "handover_days", 2)
    assert eta.phrase(_eta("ozon", 5, 5, False)) == "≈ 6–7 дней: 1–2 дня соберём и сдадим, 5 дней в пути у Ozon"


@pytest.mark.parametrize("with_conditions", [True, False])
async def test_conditions_link_without_preview_card(monkeypatch, with_conditions):
    import httpx

    from app.core.config import settings
    from app.modules.dialog import vk_client

    sent = []

    def handler(request):
        sent.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"response": 1})

    original = httpx.AsyncClient

    class Mocked(original):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(vk_client.httpx, "AsyncClient", Mocked)
    text = "Итого: 3000 ₽" + (f"\nУсловия: {settings.conditions_url}" if with_conditions else "")
    await vk_client.send_message(1, text)
    assert ("dont_parse_links" in sent[0]) is with_conditions
