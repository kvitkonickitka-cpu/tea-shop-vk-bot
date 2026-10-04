"""Задача 3: пункты с номерами, выбор из показанного списка, один ход до счёта."""

from __future__ import annotations

import time
from types import SimpleNamespace as NS

import pytest

from app.core.config import settings
from app.messages import templates
from app.modules.catalog import service as catalog_service
from app.modules.delivery import cdek_client, ozon_client, ozon_quote
from app.modules.orders import conversation, points, state
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service, yookassa_client
from tests.test_auto_invoice import said, tool_use

PEER = 9940
KRD = [
    NS(id=11, address="Краснодар, Ставропольская улица, 230"),
    NS(id=12, address="Краснодар, Ставропольская улица, 159"),
    NS(id=13, address="Краснодар, Красная улица, 176"),
    NS(id=14, address="Краснодар, Северная улица, 326"),
    NS(id=15, address="Краснодар, Лузана улица, 40"),
]
SHOWN = [{"n": 1, "id": 11, "address": KRD[0].address}, {"n": 2, "id": 12, "address": KRD[1].address},
         {"n": 3, "id": 13, "address": KRD[2].address}]


@pytest.mark.parametrize("answer, expected", [
    ("1", 1), ("2.", 2), ("№3", 3), ("второй", 2), ("давайте третий", 3),
    ("на Красной", 3), ("Ставропольская 159", 2), ("красная 176", 3),
])
def test_choose_from_shown(answer, expected):
    assert points.choose(answer, SHOWN)["n"] == expected


@pytest.mark.parametrize("answer", ["5", "на Ставропольской", "Луговая 5", ""])
def test_choose_refuses_what_is_not_shown(answer):
    assert points.choose(answer, SHOWN) is None


def test_short_label():
    assert points.short("Краснодар, Ставропольская улица, 230") == "Ставропольская ул., 230"
    assert len(points.short("Краснодар, " + "очень длинная улица " * 5 + ", 1")) == 40


@pytest.fixture
def ozon(monkeypatch):
    box = {"search": [], "priced": []}

    async def picked(draft, city, hint=""):
        box["search"].append(hint)
        rows = [p for p in KRD if hint and hint.split()[0].lower()[:6] in p.address.lower()]
        if hint and rows:
            return ozon_quote.Picked(rows, len(rows), len(rows), True)
        return ozon_quote.Picked(KRD, len(KRD), len(KRD), not hint)

    async def price(draft, point_id):
        box["priced"].append(point_id)
        return ozon_client.Quote(delivery_cost=110.0 + point_id % 10, insurance_cost=0, days=5)

    monkeypatch.setattr(conversation.ozon_quote, "is_ready", lambda: True)
    monkeypatch.setattr(conversation, "_ozon_points", picked)
    monkeypatch.setattr(conversation, "_ozon_price", price)
    monkeypatch.setattr(settings, "free_delivery_threshold", "")
    return box


async def fresh_draft(**details):
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery", details=details,
    ))


async def test_street_puts_its_points_first_with_prices(clean, ozon):
    await fresh_draft()
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"}
    )
    draft = await state.get_draft(PEER)
    shown = draft.details["shown_points"]
    assert [p["id"] for p in shown] == [11, 12]
    assert "1) Краснодар, Ставропольская улица, 230 — 111 ₽; 2) Краснодар, Ставропольская улица, 159 — 112 ₽" in result.tool_result
    assert "ozon_point_id" not in draft.details
    assert "Выберите пункт и одним сообщением пришлите ФИО, телефон и почту — сразу пришлю счёт" in result.tool_result


async def test_city_only_in_a_big_city_asks_for_the_point(clean, ozon):
    await fresh_draft()
    result = await conversation._execute_set_delivery_method(PEER, {"method": "ozon_pvz", "address": "Краснодар"})
    draft = await state.get_draft(PEER)
    # Пять пунктов в городе — список не показываем, просим адрес пункта.
    assert "shown_points" not in draft.details and draft.details["point_asked"]
    assert templates.ask_point_address("Ozon") in result.tool_result
    assert "Предварительная стоимость доставки" in result.tool_result
    assert ozon["priced"] == [11]  # цена «около» — по одному пункту
    assert draft.delivery_cost == 111 and "ozon_point_id" not in draft.details
    # Описание черновика на следующем ходу напомнит модели, чего ждём.
    assert "клиента попросили назвать улицу и дом пункта" in conversation._describe_draft(draft)

    # Клиент назвал улицу — пункты рядом, с номерами.
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская 230"}
    )
    draft = await state.get_draft(PEER)
    assert [p["id"] for p in draft.details["shown_points"]] == [11, 12] and "point_asked" not in draft.details


async def test_city_only_in_a_small_town_shows_all(clean, ozon, monkeypatch):
    async def few(draft, city, hint=""):
        return ozon_quote.Picked(KRD[:3], 3, 3, not hint)

    monkeypatch.setattr(conversation, "_ozon_points", few)
    await fresh_draft()
    await conversation._execute_set_delivery_method(PEER, {"method": "ozon_pvz", "address": "Крымск"})
    assert len((await state.get_draft(PEER)).details["shown_points"]) == 3


async def test_single_found_point_is_not_fixed(clean, ozon, monkeypatch):
    # Прежнее правило — при выключенном «один пункт на улице — сразу счёт».
    monkeypatch.setattr(settings, "single_point_instant_enabled", False)
    await fresh_draft()
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Лузана 40"}
    )
    assert "не считай его выбранным" in result.tool_result
    assert "ozon_point_id" not in (await state.get_draft(PEER)).details


async def test_number_picks_from_shown_list(clean, ozon):
    await fresh_draft()
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"}
    )
    searches = len(ozon["search"])
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "второй"}
    )
    draft = await state.get_draft(PEER)
    assert draft.details["ozon_point_id"] == 12 and len(ozon["search"]) == searches
    assert "Способ доставки зафиксирован: Ozon, пункт выдачи: Краснодар, Ставропольская улица, 159" in result.tool_result
    assert "shown_points" not in draft.details


async def test_unknown_address_is_not_accepted(clean, ozon):
    await fresh_draft()
    await conversation._execute_set_delivery_method(PEER, {"method": "ozon_pvz", "address": "Краснодар"})
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Луговая 5"}
    )
    assert "«Луговая 5» в нашем списке не нашёлся" in result.tool_result
    # Большой город — и после ненайденного адреса просим адрес снова, а не список.
    assert templates.ask_point_address("Ozon") in result.tool_result
    assert "ozon_point_id" not in (await state.get_draft(PEER)).details


async def test_cdek_list_and_choice(clean, monkeypatch):
    city = [cdek_client.DeliveryPoint(code=f"KSD{i}", address=f"ул. Красная, {i}", work_time="") for i in range(1, 7)]

    async def city_points(name):
        return city

    async def delivery(draft, method, address, delivery_point=None):
        return NS(code=136, period_min=3, period_max=4), 397.0

    monkeypatch.setattr(cdek_client, "city_points", city_points)
    monkeypatch.setattr(conversation, "_cdek_delivery", delivery)
    await fresh_draft()
    result = await conversation._execute_set_delivery_method(PEER, {"method": "cdek_pvz", "address": "Краснодар"})
    assert templates.ask_point_address("СДЭК") in result.tool_result  # шесть пунктов — просим адрес
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "cdek_pvz", "address": "Краснодар", "pickup_point": "Красная"}
    )
    assert "1) ул. Красная, 1; 2) ул. Красная, 2; 3) ул. Красная, 3; 4) ул. Красная, 4" in result.tool_result
    await conversation._execute_set_delivery_method(
        PEER, {"method": "cdek_pvz", "address": "Краснодар", "pickup_point": "3"}
    )
    assert (await state.get_draft(PEER)).details["delivery_point"] == "KSD3"


async def test_point_and_recipient_in_one_message_end_with_invoice(clean, ozon, monkeypatch):
    payments = []

    async def create_payment(draft, order_key, attempt=1):
        payments.append(draft.details["ozon_point_id"])
        return yookassa_client.Payment(
            id="pay-1", status="pending", paid=False, confirmation_url="https://yoomoney.ru/checkout/1",
            receipt_registration="", test=False, amount=draft.items_total + draft.delivery_cost,
        )

    rounds = []

    async def converse(messages, system_prompt, tools):
        rounds.append(1)
        return NS(stop_reason="tool_use", content=[
            NS(type="tool_use", id="t1", name="set_delivery_method",
               input={"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "1"}),
            NS(type="tool_use", id="t2", name="set_recipient",
               input={"name": "Иванов Иван", "phone": "89001234567", "email": "ivanov@mail.ru"}),
        ])

    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    monkeypatch.setattr(payment_service, "create_payment", create_payment)
    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    monkeypatch.setattr(catalog_service, "load_items", lambda: [
        {"name": "Те Гуань Инь (тест)", "price": 1500, "in_stock": True}])
    await fresh_draft()
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"}
    )

    reply = await conversation.handle_turn(PEER, "1, Иванов Иван, 89001234567, ivanov@mail.ru")
    assert len(rounds) == 1 and payments == [11]
    assert "Доставка: пункт выдачи Ozon, Краснодар, Ставропольская улица, 230 — 111 ₽" in reply
    assert "Оплатить: https://yoomoney.ru/checkout/1" in reply
