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
