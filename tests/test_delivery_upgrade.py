"""Задача 2: дешёвая доставка первой, быстрая — второй строкой, выше порога — доплата."""

from __future__ import annotations

import time

import pytest

from app.core.config import settings
from app.messages import templates
from app.modules.delivery import cdek_client
from app.modules.orders import conversation, state, upgrade
from app.modules.orders.state import OrderDraft
from app.modules.payment import yookassa_client
from tests.test_pickup_choice import PEER, ozon  # noqa: F401 — фикстура

OZON_5 = {"carrier": "ozon", "min": 5, "max": 5, "working": False}
CDEK_2_3 = {"carrier": "cdek", "min": 2, "max": 3, "working": True}


def row(carrier, cost, eta_):
    return upgrade.quote(carrier, cost, eta_)


def test_cheapest_first_and_ozon_wins_a_tie():
    ozon_, cdek = row("ozon", 117, OZON_5), row("cdek", 245, CDEK_2_3)
    assert upgrade.cheapest([cdek, ozon_]) is ozon_
    # Равная цена — Ozon первым, даже если СДЭК быстрее.
    tie = row("cdek", 117, CDEK_2_3)
    assert upgrade.cheapest([tie, ozon_]) is ozon_
    # Дешевле СДЭК — он и первый: порядок по цене, а не по перевозчику.
    cheap_cdek = row("cdek", 99, CDEK_2_3)
    assert upgrade.cheapest([ozon_, cheap_cdek]) is cheap_cdek


def test_faster_only_when_a_day_earlier():
    ozon_ = row("ozon", 117, OZON_5)                                    # ≈ 10 октября
    assert upgrade.faster([ozon_, row("cdek", 245, CDEK_2_3)])["carrier"] == "cdek"   # ≈ 7–8 октября
    same_day = {"carrier": "cdek", "min": 4, "max": 4, "working": True}  # пн + 4 рабочих = 9-е…
    late = {"carrier": "cdek", "min": 5, "max": 5, "working": True}      # ≈ 12 октября
    assert upgrade.faster([ozon_, row("cdek", 245, late)]) is None
    # 9 октября — на день раньше 10-го: показываем.
    assert upgrade.faster([ozon_, row("cdek", 245, same_day)]) is not None
    # Срока нет — «быстрее» не обещаем.
    assert upgrade.faster([ozon_, row("cdek", 245, None)]) is None


def test_option_texts_word_for_word():
    below = templates.delivery_options(
        base_name="Ozon", base_price=117, base_when="получите ≈ 10 октября",
        fast_name="СДЭК", fast_price=245, fast_when="получите ≈ 8 октября",
    )
    assert below == "Ozon — 117 ₽, получите ≈ 10 октября. Нужно быстрее — СДЭК 245 ₽, получите ≈ 8 октября"
    above = templates.delivery_options(
        base_name="Ozon", base_price=0, base_when="получите ≈ 10 октября",
        fast_name="СДЭК", fast_price=128, fast_when="получите ≈ 8 октября", free=True,
    )
    assert above == ("Доставка Ozon — бесплатно, получите ≈ 10 октября. "
                     "Нужно быстрее — СДЭК с доплатой 128 ₽, получите ≈ 8 октября")
    alone = templates.delivery_options(base_name="Ozon", base_price=117, base_when="получите ≈ 10 октября")
    assert alone == "Ozon — 117 ₽, получите ≈ 10 октября"


@pytest.fixture
def cdek(monkeypatch):
    calls = []

    async def quote(draft, method, address, delivery_point=None):
        calls.append((method, address))
        price = 397.0 if method == "cdek_courier" else 245.0
        return cdek_client.Tariff(136, "Посылка склад-склад", 200.0, 2, 3, 4), price

    async def points(city):
        return [cdek_client.DeliveryPoint(code="KRD1", address="Красная, 1", work_time="")]

    monkeypatch.setattr(conversation, "_cdek_delivery", quote)
    monkeypatch.setattr(conversation.cdek_client, "city_points", points)
    return calls


async def draft_for(total: float):
    await state.set_draft(PEER, OrderDraft(
        # Цена — как в таблице: перед счётом цены перепроверяются по ней.
        items=[{"name": "Те Гуань Инь", "quantity": int(total // 1500), "price": 1500}], items_total=total,
        stage="awaiting_delivery", details={},
    ))


async def test_below_threshold_both_lines_and_full_price(clean, ozon, cdek):  # noqa: F811
    await draft_for(1500)
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Красная"})
    assert ("«Ozon — 113 ₽, получите ≈ 10 октября. Нужно быстрее — СДЭК 245 ₽, получите ≈ 7–8 октября»"
            in result.tool_result)
    draft = await state.get_draft(PEER)
    assert draft.delivery_cost == 113 and upgrade.SURCHARGE not in draft.details
    # Выбрал СДЭК — платит его цену целиком: порог не пройден.
    await conversation._execute_set_delivery_method(PEER, {"method": "cdek_pvz", "address": "Краснодар"})
    draft = await state.get_draft(PEER)
    assert draft.delivery_cost == 245 and "carrier_delivery_cost" not in draft.details
    # Второй раз СДЭК не считали: свежий расчёт Ozon лежит в черновике.
    assert [c for c in cdek if c[0] == "cdek_pvz"] == [("cdek_pvz", "Краснодар")] * 2


async def test_above_threshold_cheapest_free_faster_with_surcharge(clean, ozon, cdek, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "free_delivery_threshold", "3000")
    await draft_for(3000)
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Красная"})
    assert ("«Доставка Ozon — бесплатно, получите ≈ 10 октября. Нужно быстрее — СДЭК с доплатой 132 ₽, "
            "получите ≈ 7–8 октября»") in result.tool_result
    assert (await state.get_draft(PEER)).delivery_cost == 0

    result = await conversation._execute_set_delivery_method(PEER, {"method": "cdek_pvz", "address": "Краснодар"})
    draft = await state.get_draft(PEER)
    # Доплата — разница с самым дешёвым, настоящая цена СДЭКа — для отправления.
    assert draft.delivery_cost == 132 and draft.details[upgrade.SURCHARGE] == 132
    assert draft.details["carrier_delivery_cost"] == 245
    assert "«с доплатой 132 ₽»" in result.tool_result
    assert "дешевле Ozon — на 132 ₽" in result.tool_result
    # В чеке строка доставки — ровно доплата, сумма чека = сумма платежа.
    rows = yookassa_client.receipt_items(draft.items, draft.delivery_cost, draft.delivery_label)
    assert sum(float(r["amount"]["value"]) * r["quantity"] for r in rows) == 3000 + 132
    assert float(rows[-1]["amount"]["value"]) == 132


async def test_single_carrier_quoted_stays_free(clean, ozon, monkeypatch):  # noqa: F811
    async def down(*args, **kwargs):
        raise cdek_client.CdekError("нет связи")

    monkeypatch.setattr(conversation, "_cdek_delivery", down)
    monkeypatch.setattr(settings, "free_delivery_threshold", "3000")
    await draft_for(3000)
    result = await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Красная"})
    draft = await state.get_draft(PEER)
    assert draft.delivery_cost == 0 and "Нужно быстрее" not in result.tool_result


async def test_free_cheapest_carries_no_receipt_line(clean, ozon, cdek, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "free_delivery_threshold", "3000")
    await draft_for(3000)
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Красная"})
    draft = await state.get_draft(PEER)
    rows = yookassa_client.receipt_items(draft.items, draft.delivery_cost, draft.delivery_label)
    assert len(rows) == 1


async def test_stale_cheapest_is_requoted_before_invoice(clean, ozon, cdek, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "free_delivery_threshold", "3000")
    await draft_for(3000)
    await conversation._execute_set_delivery_method(
        PEER, {"method": "ozon_pvz", "address": "Краснодар", "pickup_point": "Красная"})
    await conversation._execute_set_delivery_method(PEER, {"method": "cdek_pvz", "address": "Краснодар"})
    draft = await state.get_draft(PEER)
    # Прошло больше получаса — Ozon подешевел: доплата выросла, итог изменился.
    old = time.time() - 3600
    draft.details["quoted_at"] = old
    for quote in draft.details[upgrade.KEY]["quotes"].values():
        quote["at"] = old
    draft.details["delivery_point"] = "KRD1"
    await state.set_draft(PEER, draft)

    async def cheaper(draft, point_id):
        from app.modules.delivery import ozon_client

        return ozon_client.Quote(delivery_cost=100.0, insurance_cost=0, days=5)

    monkeypatch.setattr(conversation, "_ozon_price", cheaper)
    draft = await state.get_draft(PEER)
    note = await conversation._refresh_before_invoice(PEER, draft)
    assert draft.delivery_cost == 145
    assert note is not None and "итог изменился" in note


async def test_flag_off_threshold_covers_any_carrier(clean, ozon, cdek, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "delivery_upgrade_pricing_enabled", False)
    monkeypatch.setattr(settings, "free_delivery_threshold", "3000")
    await draft_for(3000)
    result = await conversation._execute_set_delivery_method(PEER, {"method": "cdek_pvz", "address": "Краснодар"})
    draft = await state.get_draft(PEER)
    assert draft.delivery_cost == 0 and "Нужно быстрее" not in result.tool_result
    assert upgrade.KEY not in draft.details


def test_summary_names_the_surcharge():
    text = templates.invoice_summary(
        order_id=12, items=[{"name": "Те Гуань Инь", "quantity": 2, "price": 1500}],
        delivery_method="cdek_pvz", delivery_label="СДЭК, пункт выдачи: Красная, 1", delivery_cost=132,
        name="Иванов Иван", phone="+79001234567", email="a@b.ru", total=3132, link="https://pay",
        surcharge=True,
    )
    assert "Доставка: пункт выдачи СДЭК, Красная, 1 — доплата 132 ₽" in text
