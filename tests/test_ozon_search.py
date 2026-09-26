"""Поиск пункта Ozon: город — целым словом, без потолка из соседних городов."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from app.modules.delivery import ozon_catalog
from app.modules.delivery.models import OzonDeliveryPoint


async def _add(db, rows):
    async with db() as session:
        await session.execute(
            insert(OzonDeliveryPoint).values([
                {"id": pid, "address": address, "search_text": ozon_catalog.normalize(address),
                 "is_active": True, "kind": "pvz"}
                for pid, address in rows
            ])
        )
        await session.commit()


async def _clear(db):
    async with db() as session:
        await session.execute(OzonDeliveryPoint.__table__.delete())
        await session.commit()


async def test_krai_does_not_crowd_out_the_city(clean):
    """26.09.2026: на Ставропольской нашлось два пункта из примерно десяти.

    `LIKE '%краснодар%'` поднимал весь край, первые 1000 строк без порядка
    уходили соседним городам, и краснодарские до отбора не доживали.
    """
    await _clear(clean)
    krai = [
        (i, f"Россия, Краснодарский край, г Анапа, ул Ленина, д {i}")
        for i in range(1, 1501)
    ]
    city = [
        (5000 + i, f"Россия, Краснодарский край, г Краснодар, ул Ставропольская, д {100 + i}")
        for i in range(10)
    ]
    other_street = [(6000, "Россия, Краснодарский край, г Краснодар, ул Красная, д 1")]
    await _add(clean, krai + city + other_street)

    found = await ozon_catalog.search("Краснодар", "Ставропольская", limit=20)
    assert found.hint_matched
    assert found.total == 10
    assert {p.id for p in found.points} == {pid for pid, _ in city}

    whole_city = await ozon_catalog.search("Краснодар", "", limit=50)
    assert whole_city.total == 11
    assert all("Анапа" not in p.address for p in whole_city.points)
    await _clear(clean)


async def test_named_street_survives_a_big_city(clean):
    """Пунктов города больше потолка — названная улица всё равно в выборке."""
    await _clear(clean)
    rows = [(i, f"г Москва, ул Профсоюзная, д {i}") for i in range(1, 1201)]
    rows.append((9999, "г Москва, ул Тверская, д 7"))
    await _add(clean, rows)

    found = await ozon_catalog.search("Москва", "на Тверской", limit=5)
    assert found.hint_matched and [p.id for p in found.points] == [9999]
    await _clear(clean)


def test_word_regex_keeps_every_form_of_the_word():
    import re

    pattern = re.compile(ozon_catalog._word_regex(ozon_catalog.stem("краснодар")))
    assert pattern.search("г краснодар ставропольская 230")
    assert pattern.search("краснодара")
    assert not pattern.search("краснодарский анапа")


async def test_failed_price_falls_back_to_the_next_point(clean, monkeypatch):
    """Checkout отказал по первому пункту — считаем по следующему, не шлём в СДЭК."""
    from types import SimpleNamespace

    from app.modules.delivery import ozon_client, ozon_quote
    from app.modules.orders import conversation, state
    from app.modules.orders.state import OrderDraft

    peer = 9900
    points = [
        SimpleNamespace(id=93999, address="Краснодар, Ставропольская улица, 159"),
        SimpleNamespace(id=88405, address="Краснодар, Ставропольская улица, 129"),
        SimpleNamespace(id=95166, address="Краснодар, Ставропольская улица, 268"),
    ]

    async def picked(draft, city, hint=""):
        return ozon_quote.Picked(points, 3, 22, True)

    async def price(draft, point_id):
        if point_id == 93999:
            raise RuntimeError("")
        return ozon_client.Quote(delivery_cost=107.0, insurance_cost=10.0, days=5)

    monkeypatch.setattr(conversation.ozon_quote, "is_ready", lambda: True)
    monkeypatch.setattr(conversation, "_ozon_points", picked)
    monkeypatch.setattr(conversation, "_ozon_price", price)
    await state.set_draft(peer, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 100}],
        items_total=100.0, stage="awaiting_delivery",
    ))

    result = await conversation._execute_set_delivery_method(
        peer, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Ставропольская"}
    )

    assert "недоступен" not in result.tool_result
    assert "159" not in result.tool_result
    assert "129" in result.tool_result and "268" in result.tool_result
    assert (await state.get_draft(peer)).delivery_cost == 117.0
    # Список, которым пользуется вызывающий, не испорчен.
    assert len(points) == 3
