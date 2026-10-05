"""Прогон 05.10.2026: старая кнопка, плавающий список пунктов, 400 от Claude, кнопки не к месту."""

from __future__ import annotations

from types import SimpleNamespace as NS

from app.modules.delivery import cdek_client, ozon_quote
from app.modules.dialog import claude_client
from app.modules.orders import conversation, points, state
from app.modules.orders.state import OrderDraft
from tests.test_auto_invoice import said
from tests.test_pickup_choice import KRD
from tests.test_vk_buttons import FULL, PEER, listing, say, world  # noqa: F401 — фикстура


async def test_stale_email_button_after_invoice_does_not_repeat_the_edit(clean, world):
    await listing()
    version = (await state.get_draft(PEER)).details["version"]
    await say("1", 1, {"a": "pt", "n": 1, "v": version})
    assert world["payments"] == [1]
    # Счёт выставлен; клиент жмёт кнопку под старым сообщением.
    await say("Да, @gmail.com", 2, {"a": "email_yes", "v": version})
    assert world["sent"][-1][0] == (
        "Эта кнопка уже неактуальна. Актуальная ссылка на оплату — в последнем сообщении со счётом."
    )
    assert world["payments"] == [1] and world["model"] == []


async def test_same_query_keeps_the_same_list(clean, world, monkeypatch):
    calls = []

    async def drifting(draft, city, hint=""):
        calls.append(hint)
        rows = KRD if len(calls) == 1 else KRD[2:]
        return ozon_quote.Picked(rows, len(rows), len(rows), True)

    monkeypatch.setattr(conversation, "_ozon_points", drifting)
    await listing()
    first = (await state.get_draft(PEER)).details["shown_points"]
    # Модель вызвала инструмент ещё раз с тем же городом и улицей.
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская улица"})
    assert (await state.get_draft(PEER)).details["shown_points"] == first and len(calls) == 1
    # Другая улица — новый поиск.
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Мира"})
    assert len(calls) == 2


async def test_list_says_full_when_nothing_else(clean, world, monkeypatch):
    async def two(draft, city, hint=""):
        return ozon_quote.Picked(KRD[:2], 2, 2, True)

    monkeypatch.setattr(conversation, "_ozon_points", two)
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery"))
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"})
    assert "не весь список" not in result.tool_result and "не говори, что список неполный" in result.tool_result


def test_free_delivery_list_has_no_prices():
    shown = [{"n": 1, "address": "Тверская ул., 6", "price": 129.0}]
    text = conversation._points_instruction("ozon_pvz", "Москва", shown, True, "", free=True)
    assert "129" not in text and "Цены у пунктов не называй" in text


async def test_point_buttons_only_under_a_reply_that_shows_points(clean, world):
    await listing()
    world["script"] = [said("СДЭК: пункт выдачи или курьер?")]
    await say("давайте сдэк", 1)
    text, board = world["sent"][-1]
    assert board is None or not any(
        row[0]["action"]["label"][:2] in ("1.", "2.") for row in board["buttons"]
    )


async def test_words_only_keeps_tools_and_forbids_calls(monkeypatch):
    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        return NS(stop_reason="end_turn", content=[NS(type="text", text="ок")])

    monkeypatch.setattr(claude_client._client.messages, "create", create)
    tool = {"name": "x", "description": "", "input_schema": {"type": "object"}}
    await claude_client.converse(
        [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "  "},
         {"role": "user", "content": "ещё"}],
        "s", [tool], words_only=True,
    )
    # Без инструментов запрос с tool_use в истории — 400; запрещаем вызовы, а не убираем их.
    assert sent["tools"] == [tool] and sent["tool_choice"] == {"type": "none"}
    assert [m["content"] for m in sent["messages"]] == ["привет", "ещё"]


def test_cdek_address_without_repeats_and_hours_on_buttons():
    assert cdek_client.tidy_address("Россия, Москва, Москва, ул. Тверская, 9, стр.7, 7") == (
        "Россия, Москва, ул. Тверская, 9, стр.7"
    )
    assert points.short("Москва, ул. Тверская, 9, стр.7 (Пн-Пт 08:00-20:00)", 34) == "ул. Тверская, 9, стр.7"


# --- Перепроверка 05.10.2026 на ревизии 8e1e28b ---


async def test_repeated_number_keeps_the_recorded_point(clean, world, monkeypatch):
    """09:56: пункт записан, модель снова передала «1» — бот искал дома № 1 по Москве."""
    searches = []
    real = conversation._ozon_points

    async def counting(draft, city, hint=""):
        searches.append(hint)
        return await real(draft, city, hint)

    await listing(recipient=False)
    monkeypatch.setattr(conversation, "_ozon_points", counting)
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "1"})
    chosen = (await state.get_draft(PEER)).details["ozon_point_id"]
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "1"})
    draft = await state.get_draft(PEER)
    assert draft.details["ozon_point_id"] == chosen and searches == []


async def test_number_without_list_or_point_is_not_a_street(clean, world, monkeypatch):
    hints = []

    async def picked(draft, city, hint=""):
        hints.append(hint)
        return ozon_quote.Picked(KRD, len(KRD), len(KRD), True)

    monkeypatch.setattr(conversation, "_ozon_points", picked)
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery"))
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "второй"})
    assert hints == [""]


async def test_city_named_with_the_product_is_priced_right_away(clean, world):
    """09:52: «Хочу две пачки Да Хун Пао, Москва» — бот просил улицу без цены и даты."""
    result = await conversation._execute_propose_order(
        PEER, {"items": [{"name": "Да Хун Пао", "quantity": 2}], "delivery_hint": "Москва"})
    text = result if isinstance(result, str) else result.tool_result
    assert "вызови set_delivery_method" in text and "Город не переспрашивай" in text


async def test_points_list_demands_price_and_date(clean, world):
    """09:48: список на «Тверская» — с ценой, но без даты."""
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery"))
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"})
    assert "ОБЯЗАТЕЛЬНО" in result.tool_result and "получите" in result.tool_result
