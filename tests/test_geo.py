"""Задача 4: геопозиция вместо адреса с карты — ближайшие пункты кодом, координаты только меткой."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text, update

from app import privacy
from app.messages import keyboard as keyboards
from app.messages.models import FunnelEvent
from app.modules.delivery import ozon_catalog, ozon_client
from app.modules.delivery.models import OzonDeliveryPoint
from app.modules.dialog import history as dialog_history, inbound
from app.modules.dialog.models import InboundMessage
from app.modules.orders import conversation, geo, state
from app.modules.orders.state import OrderDraft
from app.privacy.models import PiiEntry
from tests.test_auto_invoice import said
from tests.test_vk_buttons import FULL, PEER, world  # noqa: F401 — фикстура

# Клиент стоит у Красной, 176 в Краснодаре.
HERE = (45.035470, 38.975313)
POINTS = [
    # id, адрес, широта, долгота — ≈ 0,6 км, ≈ 1,3 км, ≈ 3 км, ≈ 60 км (Горячий Ключ — дальше радиуса).
    (21, "Россия, Краснодарский край, Краснодар, улица Благоева, 2/1", 45.0408, 38.9741),
    (22, "Россия, Краснодарский край, Краснодар, Красная улица, 109", 45.0466, 38.9800),
    (23, "Россия, Краснодарский край, Краснодар, улица Северная, 326", 45.0620, 38.9790),
    (24, "Россия, Краснодарский край, Горячий Ключ, улица Ленина, 1", 44.6340, 39.1360),
]


def geo_message(n: int, lat=HERE[0], lon=HERE[1], payload: dict | None = None) -> dict:
    message = {"peer_id": PEER, "text": "", "conversation_message_id": n,
               "geo": {"type": "point", "coordinates": {"latitude": lat, "longitude": lon},
                       "place": {"title": "Краснодар", "city": "Краснодар", "country": "Россия"}}}
    if payload is not None:
        message["payload"] = json.dumps(payload)
    return message


@pytest.fixture
async def catalog(clean, monkeypatch):
    async def all_allowed(*, delivery_point_ids, **kwargs):
        return set(delivery_point_ids)

    async def price(draft, point_id):
        return ozon_client.Quote(delivery_cost=100.0 + point_id, insurance_cost=0, days=5)

    monkeypatch.setattr(ozon_client, "available_points", all_allowed)
    monkeypatch.setattr(conversation, "_ozon_price", price)
    monkeypatch.setattr(ozon_catalog, "_COORDS_CHECKED", None)

    ids = [row[0] for row in POINTS]

    async def drop():
        from sqlalchemy import delete

        async with clean() as session:
            await session.execute(delete(OzonDeliveryPoint).where(OzonDeliveryPoint.id.in_(ids)))
            await session.commit()

    async def fill():
        await drop()
        async with clean() as session:
            session.add_all([OzonDeliveryPoint(id=i, address=a, search_text=ozon_catalog.normalize(a),
                                               is_active=True, kind="pvz", latitude=lat, longitude=lon)
                             for i, a, lat, lon in POINTS])
            await session.commit()

    yield fill
    # Каталог Ozon между тестами не чистится: свои пункты с координатами
    # убираем, иначе соседние тесты увидят кнопку геопозиции.
    await drop()
    ozon_catalog._COORDS_CHECKED = None


async def draft_waiting():
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery", details={}))


def test_scrub_catches_coordinates_but_not_prices():
    clean_text, count = privacy.scrub("я тут 45.035470, 38.975313 — рядом")
    assert clean_text == "я тут [REDACTED] — рядом" and count == 1
    assert privacy.scrub("итого 1500.00, доставка 117.00")[1] == 0
    assert privacy.scrub("широта 95.123456 долгота 38.975313")[1] == 0


def test_distance_text():
    assert geo.distance_text(560) == "≈ 600 м"
    assert geo.distance_text(40) == "≈ 100 м"
    assert geo.distance_text(1260) == "≈ 1,3 км"


def test_ozon_coordinates_are_read_defensively():
    assert ozon_client.coordinates_of({"coordinates": {"latitude": 45.04, "longitude": 38.97}}) == (45.04, 38.97)
    assert ozon_client.coordinates_of({"location": {"lat": "45.04", "lon": "38.97"}}) == (45.04, 38.97)
    assert ozon_client.coordinates_of({"coordinates": {"latitude": 0, "longitude": 0}}) == (None, None)
    assert ozon_client.coordinates_of({}) == (None, None)


async def test_near_sorts_and_cuts_by_radius(catalog):
    await catalog()
    found = await ozon_catalog.near(*HERE, radius_km=10)
    assert [row.id for row, _ in found] == [21, 22, 23]
    assert 500 < found[0][1] < 700


async def test_geo_in_dialog_shows_nearest_points_without_the_model(catalog, world):
    await catalog()
    await keyboards.remember_client(PEER, FULL)
    await draft_waiting()
    assert await geo.offer_for(PEER)
    await inbound.accept("ev1", geo_message(1, payload={"a": "geo"}), FULL)

    text_, board = world["sent"][-1]
    # Цена и срок — из подмены Ozon в фикстуре world: 121 ₽ и 6 дней.
    assert text_.startswith("Ближайшие пункты выдачи Ozon, получите ≈ 11 октября:\n"
                            "1) Россия, Краснодарский край, Краснодар, улица Благоева, 2/1 — ≈ 600 м, 121 ₽\n"
                            "2) Россия, Краснодарский край, Краснодар, Красная улица, 109 — ≈ 1,3 км, 121 ₽")
    assert "Выберите пункт выдачи и одним сообщением пришлите ФИО, телефон и почту" in text_
    assert [b["action"]["label"] for row in board["buttons"] for b in row][:2] == [
        "1. ул. Благоева, 2/1", "2. Красная ул., 109"]
    assert world["model"] == []
    draft = await state.get_draft(PEER)
    assert [p["id"] for p in draft.details["shown_points"]] == [21, 22, 23]
    assert draft.details["address"] == "Краснодар" and draft.delivery_method == "ozon_pvz"

    # Координат нет нигде, кроме зашифрованного хранилища.
    async with world_db() as session:
        stored = [row.message for row in (await session.execute(select(InboundMessage))).scalars()]
        events = [(row.event, row.data) for row in (await session.execute(select(FunnelEvent))).scalars()]
        vault = (await session.execute(select(PiiEntry).where(PiiEntry.kind == "GEO"))).scalars().all()
    dump = json.dumps([stored, events, [m["content"] for m in await dialog_history.get_history(PEER)]],
                      ensure_ascii=False)
    assert "45.03" not in dump and "38.97" not in dump
    assert len(vault) == 1 and "45.03" not in vault[0].value_enc
    assert ("geo_sent", {"carrier": "ozon", "found": 3}) in events
    history = await dialog_history.get_history(PEER)
    assert history[-2]["content"] == "[GEO_1] Клиент отправил геопозицию"

    # Выбор из списка дальше — обычным путём; в запросе к модели координат нет.
    seen = []

    async def converse(messages, system_prompt, tools, **_):
        seen.append(json.dumps([system_prompt, messages], ensure_ascii=False))
        return said("Записала первый пункт.")

    from app.modules.orders import conversation as conv

    conv.claude_client.converse = converse  # world уже подменил, вернёт monkeypatch
    await inbound.accept("ev2", {"peer_id": PEER, "text": "давайте где ближе, сколько идти?",
                                 "conversation_message_id": 2}, FULL)
    assert seen and all("45.03" not in s and "38.97" not in s for s in seen)
    assert any("[GEO_1]" in s for s in seen)


def world_db():
    from app.core.database import get_session_factory

    return get_session_factory()()


async def test_nothing_within_radius_asks_for_street(catalog, world):
    await catalog()
    await keyboards.remember_client(PEER, FULL)
    await draft_waiting()
    await inbound.accept("ev1", geo_message(1, lat=55.75, lon=37.62), FULL)
    assert world["sent"][-1][0] == (
        "В радиусе 10 км от вас пунктов выдачи Ozon не нашлось. Напишите город и улицу, где удобно забрать, "
        "— поищу по адресу.")
    assert (await state.get_draft(PEER)).delivery_method is None


async def test_geo_button_only_with_coordinates_and_app_support(catalog, world):
    await keyboards.remember_client(PEER, FULL)
    assert not await geo.offer_for(PEER)  # в каталоге нет координат
    await catalog()
    from app.modules.delivery import ozon_catalog as oc

    oc._COORDS_CHECKED = None
    assert await geo.offer_for(PEER)
    await keyboards.remember_client(PEER, {**FULL, "button_actions": ["text"]})
    assert not await geo.offer_for(PEER)


async def test_old_geo_is_forgotten(catalog, world):
    await keyboards.remember_client(PEER, FULL)
    label = await privacy.geo_label(PEER, *HERE)
    assert await privacy.detokenize(PEER, f"ваша [{label}]") == "ваша геопозиция"
    async with world_db() as session:
        await session.execute(update(PiiEntry).values(created_at=text("now() - interval '25 hours'")))
        await session.commit()
    assert await privacy.forget_old_geo() == 1
    assert await privacy.geo_value(PEER, label) is None
    # Метка в истории после удаления не роняет ответ.
    assert await privacy.detokenize(PEER, f"[{label}]") == "геопозиция"


async def test_storefront_lead_puts_geo_next_to_faster(catalog, world, monkeypatch):
    from app.modules.delivery import cdek_client
    from app.modules.dialog import service as dialog_service
    from tests.test_storefront_vk_flow import ADMIN

    async def cdek(draft, method, address, delivery_point=None):
        return cdek_client.Tariff(136, "Посылка склад-склад", 200.0, 2, 3, 4), 245.0

    async def no_items(order_id, user_id=None):
        raise RuntimeError("VK API error")

    async def to_manager(text, chat_id=None):
        return None

    from app.messages import manager as manager_messages
    from app.modules.orders import vk_orders_client

    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    monkeypatch.setattr(conversation, "_cdek_delivery", cdek)
    monkeypatch.setattr(vk_orders_client, "get_order_items", no_items)
    await catalog()
    await keyboards.remember_client(PEER, FULL)
    await dialog_service.handle_message_reply({"peer_id": PEER, "text": ADMIN})
    text_, board = world["sent"][-1]
    assert ("Где удобно забрать? Отправьте геопозицию кнопкой или напишите улицу — покажу ближайшие пункты."
            in text_)
    assert [[b["action"]["type"] for b in row] for row in board["buttons"]] == [["location", "text"]]
    assert board["buttons"][0][1]["action"]["label"] == "Нужно быстрее — СДЭК"
    async with world_db() as session:
        events = [(row.event, row.data) for row in (await session.execute(select(FunnelEvent))).scalars()]
    assert ("geo_button_shown", {"where": "storefront"}) in events


async def test_take_offers_geo(catalog, world):
    from app.modules.dialog import service as dialog_service  # noqa: F401
    from tests.test_vk_buttons import say

    await catalog()
    await keyboards.remember_client(PEER, FULL)
    await say("Взять Да Хун Пао", 1, {"a": "take", "n": "Да Хун Пао"})
    text_, board = world["sent"][-1]
    assert text_.endswith("Куда везти — город и улица, где удобно забрать? Или отправьте геопозицию "
                          "кнопкой ниже — покажу ближайшие пункты.")
    assert board["buttons"][-1][0]["action"]["type"] == "location"
