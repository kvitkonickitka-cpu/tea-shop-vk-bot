"""Надбавка на эквайринг к цене доставки для клиента.

ЮKassa берёт комиссию со всей суммы платежа, включая доставку. Клиенту
называем цену перевозчика плюс DELIVERY_ACQUIRING_PERCENT, а сверка с
Ozon при создании отправления идёт по цене без надбавки.
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import settings
from app.modules.delivery import cdek_client, ozon_client
from app.modules.delivery import pricing
from app.modules.orders import conversation, shipping
from app.modules.orders.state import OrderDraft


@pytest.fixture
def acquiring(monkeypatch):
    monkeypatch.setattr(settings, "delivery_acquiring_percent", 3.5)


def _draft() -> OrderDraft:
    return OrderDraft(items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 1500}], items_total=1500)


def test_price_rounds_up_to_kopeck(acquiring):
    assert pricing.for_client(117) == 121.1
    # 397,72 × 1,035 = 411,6402 — вверх, а не по правилам: недобор копейки — та же доплата магазина.
    assert pricing.for_client(397.72) == 411.65


def test_free_and_missing_price_stay_as_is(acquiring):
    assert pricing.for_client(0) == 0
    assert pricing.for_client(None) is None


def test_zero_percent_changes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "delivery_acquiring_percent", 0)
    assert pricing.for_client(397.72) == 397.72


def test_carrier_part_reverses_markup(acquiring):
    for carrier in (117, 119.63, 245, 397.72):
        assert abs(pricing.carrier_part(pricing.for_client(carrier)) - carrier) <= 0.01


async def test_cdek_price_for_client_has_markup(monkeypatch, acquiring):
    tariff = cdek_client.Tariff(code=136, name="Склад-склад", delivery_sum=320,
                                period_min=3, period_max=5, delivery_mode=4)

    async def tariffs(address, weight):
        return [tariff]

    async def total(*args, **kwargs):
        return 397.72

    monkeypatch.setattr(conversation.cdek_client, "calculate_tariffs", tariffs)
    monkeypatch.setattr(conversation.cdek_client, "cheapest", lambda found, modes: found[0])
    monkeypatch.setattr(conversation.cdek_client, "calculate_total", total)

    got_tariff, cost = await conversation._cdek_delivery(_draft(), "cdek_pvz", "Москва")
    assert got_tariff is tariff
    assert cost == 411.65


async def test_ozon_price_for_client_has_markup_on_insurance_too(monkeypatch, acquiring):
    async def price_for(point_id, *, phone, weight_grams, declared_value):
        return ozon_client.Quote(delivery_cost=112, insurance_cost=5, days=6)

    monkeypatch.setattr(conversation.ozon_quote, "price_for", price_for)
    quote = await conversation._ozon_price(_draft(), 46348)
    assert quote.total == 121.1
    assert quote.days == 6


async def test_ozon_shipment_compared_without_markup(monkeypatch, acquiring, caplog):
    class Posting:
        total = 117.0
        posting_number = "123-0001-1"

    async def create_order(**kwargs):
        return Posting()

    monkeypatch.setattr(shipping.ozon_client, "create_order", create_order)
    details = {"ozon_point_id": 46348, "recipient_name": "[NAME_1]", "recipient_phone": "+79000000000"}
    with caplog.at_level(logging.WARNING, logger=shipping.logger.name):
        number = await shipping._in_ozon(
            1, "vk1-1", [{"name": "Те Гуань Инь", "quantity": 1, "price": 1500, "weight": 100}],
            details, 1500, pricing.for_client(117),
        )
    assert number == "123-0001-1"
    assert "иначе" not in caplog.text
