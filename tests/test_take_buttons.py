"""Funnel v3, задача 2: кнопки «Взять» под консультацией."""

from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.messages import templates
from app.modules.catalog import service as catalog_service
from app.modules.orders import state, take
from tests.test_auto_invoice import said, tool_use
from tests.test_returning_client import past_order
from tests.test_vk_buttons import PEER, say, world  # noqa: F401

CATALOG = [
    {"name": "Те Гуань Инь (тест)", "price": 1500, "in_stock": True, "recommended": ["Да Хун Пао"],
     "package_sizes": ["100 г"]},
    {"name": "Да Хун Пао", "price": 1500, "in_stock": True, "recommended": [], "package_sizes": ["100 г"]},
    {"name": "Шу Пуэр", "price": 900, "in_stock": False, "recommended": []},
]
PACKS = [
    {"name": "Те Гуань Инь 50 г", "price": 800, "in_stock": True, "package_sizes": ["50 г"]},
    {"name": "Те Гуань Инь 100 г", "price": 1500, "in_stock": True, "package_sizes": ["100 г"]},
    {"name": "Да Хун Пао", "price": 1500, "in_stock": True},
    {"name": "Габа", "price": 1100, "in_stock": True},
]
ADVICE = "Из улунов советую «Те Гуань Инь» — свежий и цветочный, а для тёплого вкуса — Да Хун Пао."


def labels(board):
    return [b["action"]["label"] for row in (board or {"buttons": []})["buttons"] for b in row]


def test_names_match_whole_words_without_case_quotes_and_brackets():
    found = take.mentioned(ADVICE, CATALOG)
    assert [item["name"] for item in found] == ["Те Гуань Инь (тест)", "Да Хун Пао"]
    assert take.mentioned("ТЕ ГУАНЬ ИНЬ хорош", CATALOG)[0]["name"] == "Те Гуань Инь (тест)"
    assert take.mentioned("Да Хун Паола — это не чай", CATALOG) == []
    assert take.mentioned("Шу Пуэр сейчас лучший", CATALOG) == []  # не в наличии


def test_three_buttons_at_most_and_one_per_pack():
    found = take.mentioned("Те Гуань Инь, Да Хун Пао и Габа", PACKS)
    assert [take.label("Взять", item) for item in found] == [
        "Взять Те Гуань Инь 50 г", "Взять Те Гуань Инь 100 г", "Взять Да Хун Пао",
    ]


def test_yo_and_e_are_the_same():
    catalog = [{"name": "Цзинь Цзюнь Мэй", "price": 2000, "in_stock": True}]
    assert take.mentioned("Цзинь Цзюнь Мэй — чёрный чай", catalog)
    catalog = [{"name": "Лао Ча Тоу «Ёлочный»", "price": 900, "in_stock": True}]
    assert take.mentioned("лао ча тоу елочный", catalog)


@pytest.mark.parametrize("reply", [
    "Советую тегуанинь — цветочный.",
    "Советую Те-Гуань-Инь — цветочный.",
    "ТГИ сейчас самый свежий.",
    "Возьмите «железную богиню» — не пожалеете.",
])
def test_synonyms(reply):
    catalog = [{"name": "Те Гуань Инь (тест)", "price": 1500, "in_stock": True,
                "synonyms": ["Железная богиня милосердия", "железная богиня"]}]
    assert [item["name"] for item in take.mentioned(reply, catalog)] == ["Те Гуань Инь (тест)"]


def test_short_names_have_no_initials():
    catalog = [{"name": "Шу Пуэр", "price": 900, "in_stock": True},
               {"name": "Да Хун Пао", "price": 1500, "in_stock": True}]
    assert take.mentioned("шп и дхп", catalog) == [{**catalog[1], "_several": False}]
    assert take.mentioned("шупуэр", catalog)[0]["name"] == "Шу Пуэр"


def test_sheet_reads_synonyms():
    from app.modules.catalog import sheet

    parsed = sheet.parse_csv("Название,Цена,Синонимы\nТе Гуань Инь,1500,\"ТГИ, железная богиня\"\n")
    assert not parsed.errors and parsed.items[0]["synonyms"] == ["ТГИ", "железная богиня"]


@pytest.fixture
def shop(world, monkeypatch):
    monkeypatch.setattr(catalog_service, "load_items", lambda: CATALOG)
    return world


async def test_advice_gets_take_buttons_but_not_twice(clean, shop):
    shop["script"] = [said(ADVICE)]
    await say("посоветуйте улун", 1)
    assert labels(shop["sent"][-1][1]) == ["Взять Те Гуань Инь", "Взять Да Хун Пао"]
    shop["script"] = [said("Те Гуань Инь заваривают 80–85 °C. Да Хун Пао — 95 °C.")]
    await say("а как заваривать?", 2)
    assert shop["sent"][-1][1] is None  # тот же набор под предыдущим сообщением
    shop["script"] = [said("Есть ещё Да Хун Пао — он теплее.")]
    await say("а что потеплее?", 3)
    assert labels(shop["sent"][-1][1]) == ["Взять Да Хун Пао"]


async def test_no_buttons_after_escalation_or_medical(clean, shop):
    shop["script"] = [tool_use("escalate_to_manager", question="опт", reason="опт"),
                      said("Передала менеджеру. Пока могу посоветовать Те Гуань Инь.")]
    await say("хочу оптом", 1)
    assert shop["sent"][-1][1] is None
    shop["script"] = [said("Медицинских советов не даю — лучше спросить врача. Те Гуань Инь — просто вкусный.")]
    await say("Те Гуань Инь снижает давление?", 2)
    assert shop["sent"][-1][1] is None


async def test_take_new_client_full_path(clean, shop):
    shop["script"] = [said(ADVICE)]
    await say("посоветуйте улун", 1)
    take_button = shop["sent"][-1][1]["buttons"][0][0]["action"]
    await say(take_button["label"], 2, json.loads(take_button["payload"]))
    text, board = shop["sent"][-1]
    assert text == (
        "Записала: Те Гуань Инь — 1500 ₽.\n"
        "К нему можно добавить Да Хун Пао — 1500 ₽, до бесплатной доставки как раз не хватает 1500 ₽.\n"
        "Если нужно больше пачек — напишите сколько.\n"
        + templates.ASK_WHERE
    )
    assert labels(board) == ["Добавить Да Хун Пао"]
    draft = await state.get_draft(PEER)
    assert [row["name"] for row in draft.items] == ["Те Гуань Инь (тест)"] and draft.stage == "awaiting_delivery"
    assert len(shop["model"]) == 1  # модель звали только на совет, нажатие обработал код

    # Город и улица — дальше модель, обычным путём.
    shop["script"] = [tool_use("set_delivery_method", method="ozon_pvz", address="Краснодар",
                               pickup_point="Ставропольская"),
                      said("Пункты: 1) Ставропольская, 230 — 121 ₽ … Выберите пункт и пришлите ФИО, телефон и почту.")]
    await say("Краснодар, Ставропольская", 3)
    points = shop["sent"][-1][1]
    assert labels(points)[0].startswith("1. ")
    shop["script"] = [tool_use("set_delivery_method", method="ozon_pvz", address="Краснодар", pickup_point="1"),
                      tool_use("set_recipient", name="Иванов Иван", phone="89001234567", email="ivanov@mail.ru")]
    await say("1, Иванов Иван, 89001234567, ivanov@mail.ru", 4)
    assert shop["payments"] == [1] and "Оплатить: https://yoomoney.ru/checkout/1" in shop["sent"][-1][0]


async def test_take_returning_client_gets_the_link(clean, shop):
    await past_order(clean)
    shop["script"] = [said(ADVICE)]
    await say("что посоветуете?", 1)
    take_button = shop["sent"][-1][1]["buttons"][0][0]["action"]
    await say(take_button["label"], 2, json.loads(take_button["payload"]))
    text, board = shop["sent"][-1]
    assert text.startswith("Как в прошлый раз — проверьте, всё ли верно:") and shop["payments"] == [1]
    assert labels(board)[0] == "Оплатить 1621 ₽"


async def test_draft_gets_add_buttons_and_no_buttons_after_invoice(clean, shop):
    shop["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}]),
                      said("Записала Те Гуань Инь. Можно добавить Да Хун Пао. Куда везти?")]
    await say("беру те гуань инь", 1)
    # Кнопка допродажи из черновика важнее — она и стоит.
    assert labels(shop["sent"][-1][1]) == ["Добавить Да Хун Пао"]
    shop["script"] = [said("Да Хун Пао тоже хорош — потеплее.")]
    await say("а Да Хун Пао какой?", 2)
    add = shop["sent"][-1][1]["buttons"][0][0]["action"]
    assert add["label"] == "Добавить Да Хун Пао" and json.loads(add["payload"])["a"] == "add_item"
    await say(add["label"], 3, json.loads(add["payload"]))
    assert shop["sent"][-1][0].startswith("Добавила Да Хун Пао. Товаров на 3000 ₽.")
    assert {row["name"] for row in (await state.get_draft(PEER)).items} == {"Те Гуань Инь (тест)", "Да Хун Пао"}


async def test_old_take_button_with_a_draft_is_stale(clean, shop):
    shop["script"] = [said(ADVICE)]
    await say("посоветуйте улун", 1)
    take_button = shop["sent"][-1][1]["buttons"][0][0]["action"]
    shop["script"] = [tool_use("propose_order", items=[{"name": "Да Хун Пао", "quantity": 1}]),
                      said("Записала Да Хун Пао.")]
    await say("беру да хун пао", 2)
    shop["script"] = [said("Уже собираем Да Хун Пао.")]
    await say(take_button["label"], 3, json.loads(take_button["payload"]))
    assert any(text == templates.button_stale() for text, _ in shop["sent"])


async def test_flag_off(clean, shop, monkeypatch):
    monkeypatch.setattr(settings, "take_buttons_enabled", False)
    shop["script"] = [said(ADVICE)]
    await say("посоветуйте улун", 1)
    assert shop["sent"][-1][1] is None
