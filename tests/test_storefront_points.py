"""Funnel v3, задача 5: заказ из витрины сразу показывает пункты рядом с адресом."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.messages import keyboard as keyboards, manager as manager_messages, templates
from app.messages.models import ClientNotice
from app.modules.dialog import history as dialog_history
from app.modules.orders import address, service as orders_service, state, vk_orders_client
from tests.test_auto_invoice import said, tool_use
from tests.test_returning_client import past_order
from tests.test_vk_buttons import FULL, PEER, say, world  # noqa: F401

ADDRESS = "Россия, Краснодарский край, Краснодар, улица Ставропольская, 230, кв. 5"


@pytest.mark.parametrize("raw, expected", [
    (ADDRESS, ("Краснодар", "Ставропольская")),
    ("Москва, Тверская ул., 1", ("Москва", "Тверская")),
    ("350000, г. Краснодар, ул. Красная, 176", ("Краснодар", "Красная")),
    ("г. Уфа, пр-кт Октября, 12", ("Уфа", "Октября")),
    ("Москва, Тверская, 1", ("Москва", "Тверская")),
    ("Краснодар", ("Краснодар", "")),
    ("улица Ленина, 5", None),
    ("", None),
])
def test_city_and_street(raw, expected):
    assert address.city_and_street(raw) == expected


def labels(board):
    return [b["action"]["label"] for row in (board or {"buttons": []})["buttons"] for b in row]


@pytest.fixture
def shop(world, monkeypatch):
    world["manager"] = []
    world["order"] = {
        "id": 77, "user_id": PEER, "total_price": {"amount": 150000},
        "delivery": {"address": ADDRESS, "type": "pickup"},
        "recipient": {"name": "Петров Пётр", "phone": "8 (900) 765-43-21"},
    }

    async def to_manager(text, chat_id=None):
        world["manager"].append(text)

    async def get_order(order_id):
        return world["order"]

    async def get_items(order_id):
        return [{"item": {"title": "Те Гуань Инь (тест)"}, "quantity": 1, "price": {"amount": "150000"}}]

    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    monkeypatch.setattr(vk_orders_client, "get_order", get_order)
    monkeypatch.setattr(vk_orders_client, "get_order_items", get_items)
    return world


async def storefront(shop, *, buttons=True):
    if buttons:
        await keyboards.remember_client(PEER, FULL)
    await orders_service.handle_new_order({"id": 77})
    return shop["sent"][-1]


POINTS = (
    "Ближайшие пункты выдачи Ozon — дешевле всего, заберёте сами:\n"
    "1) Краснодар, Ставропольская улица, 230 — 121 ₽\n"
    "2) Краснодар, Ставропольская улица, 159 — 121 ₽\n"
    "3) Краснодар, Красная улица, 176 — 121 ₽\n"
    "4) Краснодар, Северная улица, 326 — 121 ₽\n"
    "Быстрее, но дороже — пункт выдачи СДЭК или курьер: напишите, если нужен он.\n"
)


async def test_recipient_from_the_order_asks_only_email(clean, shop):
    text, board = await storefront(shop)
    assert text == (
        "Заказ №77 принят: Те Гуань Инь (тест) × 1 — 1500 ₽.\n" + POINTS
        + templates.storefront_ask_email("Петров Пётр", "+79007654321") + "\n" + templates.POINTS_HINT
    )
    assert labels(board) == ["1. Ставропольская улица, 230", "2. Ставропольская улица, 159",
                             "3. Красная улица, 176", "4. Северная улица, 326"]
    draft = await state.get_draft(PEER)
    assert draft.delivery_method == "ozon_pvz" and not draft.details.get("ozon_point_id")
    assert draft.details["storefront_recipient"] == {"name": "Петров Пётр", "phone": "+79007654321"}
    # Сказанное ботом — в истории и в журнале отправок, менеджер знает о заказе.
    assert (await dialog_history.get_history(PEER))[-1]["content"] == text
    async with clean() as session:
        notices = (await session.execute(select(ClientNotice.event_type))).scalars().all()
    assert notices == [templates.STOREFRONT_ORDER]
    assert shop["manager"] and "№77" in shop["manager"][-1]

    # Пункт — кнопкой: просим только почту.
    version = (await state.get_draft(PEER)).details["version"]
    await say("1. Ставропольская улица, 230", 1, {"a": "pt", "n": 1, "v": version})
    assert shop["sent"][-1][0].endswith(templates.storefront_ask_email_only("Петров Пётр", "+79007654321"))
    assert shop["model"] == []

    # Почта — модель записывает получателя из заказа, код выставляет счёт.
    shop["script"] = [tool_use("set_recipient", name="Петров Пётр", phone="+79007654321", email="petrov@mail.ru")]
    await say("petrov@mail.ru", 2)
    assert shop["payments"] == [1] and "https://yoomoney.ru/checkout/1" in json.dumps(shop["sent"][-1][1])


async def test_no_recipient_in_the_order_asks_everything(clean, shop):
    shop["order"].pop("recipient")
    text, _ = await storefront(shop)
    assert templates.STOREFRONT_ASK_ALL in text and "storefront_recipient" not in (await state.get_draft(PEER)).details


async def test_returning_client_gets_same_data_button(clean, shop):
    await past_order(clean)
    text, board = await storefront(shop)
    assert templates.storefront_ask_last("Иванов Иван", "+79001234567", "ivanov@mail.ru") in text
    assert labels(board)[-1] == "Да, на эти данные"
    # Пункт кнопкой, затем «Да, на эти данные» — счёт без модели.
    version = (await state.get_draft(PEER)).details["version"]
    await say("1. Ставропольская улица, 230", 1, {"a": "pt", "n": 1, "v": version})
    # Под «Записала пункт» — снова кнопка: прежняя под списком устарела.
    text, board = shop["sent"][-1]
    assert text.endswith(templates.ask_last_recipient("Иванов Иван", "+79001234567", "ivanov@mail.ru"))
    same = board["buttons"][0][0]["action"]
    assert same["label"] == "Да, на эти данные"
    await say(same["label"], 2, json.loads(same["payload"]))
    assert shop["model"] == [] and shop["payments"] == [1]


async def test_no_buttons_without_client_info_keeps_text(clean, shop):
    text, board = await storefront(shop, buttons=False)
    assert board is None and templates.POINTS_HINT not in text and "1) Краснодар" in text


async def test_returning_client_without_buttons_answers_yes(clean, shop):
    await past_order(clean)
    text, board = await storefront(shop, buttons=False)
    assert board is None and "Нажмите" not in text
    assert templates.storefront_ask_last("Иванов Иван", "+79001234567", "ivanov@mail.ru", button=False) in text


async def test_unparsed_address_falls_back(clean, shop):
    shop["order"]["delivery"] = {"address": "ул. Ленина, 5"}
    text, board = await storefront(shop)
    assert board is None and "В какой город" in text and "Ближайшие пункты" not in text
    draft = await state.get_draft(PEER)
    assert draft.stage == "awaiting_delivery" and not draft.delivery_method


async def test_no_points_falls_back(clean, shop, monkeypatch):
    from app.modules.orders import conversation

    async def nothing(draft, city, hint=""):
        from app.modules.delivery import ozon_quote
        return ozon_quote.Picked([], 0, 0, True)

    monkeypatch.setattr(conversation, "_ozon_points", nothing)
    text, _ = await storefront(shop)
    assert text.startswith("Заказ №77 принят") and "Ближайшие пункты" not in text
    draft = await state.get_draft(PEER)
    assert not draft.delivery_method and draft.details["vk_order_address"] == ADDRESS


async def test_flag_off(clean, shop, monkeypatch):
    monkeypatch.setattr(settings, "storefront_direct_points_enabled", False)
    text, board = await storefront(shop)
    assert "Ближайшие пункты" not in text and board is None
    assert not (await state.get_draft(PEER)).delivery_method


async def test_city_without_street_asks_for_the_point(clean, shop):
    shop["order"]["delivery"] = {"address": "Россия, Краснодар"}
    text, board = await storefront(shop)
    assert text == "\n".join([
        "Заказ №77 принят: Те Гуань Инь (тест) × 1 — 1500 ₽.",
        "Дешевле всего — пункт выдачи Ozon в городе Краснодар — около 121 ₽, заберёте сами. "
        "Быстрее, но дороже — пункт выдачи СДЭК или курьер: напишите, если нужен он.",
        templates.ask_point_address("Ozon"),
        templates.storefront_with_point_email("Петров Пётр", "+79007654321"),
    ])
    assert board is None  # пунктов в тексте нет — и кнопок под ними нет
    draft = await state.get_draft(PEER)
    assert draft.details["point_asked"] and "shown_points" not in draft.details
