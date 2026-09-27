"""Задача 2: допродажа из «С чем советуем» и порог бесплатной доставки."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.modules.catalog import service as catalog_service, sheet
from app.modules.orders import conversation, state
from app.modules.orders.state import OrderDraft
from app.modules.payment import yookassa_client

PEER = 9960

CATALOG = [
    {"name": "Те Гуань Инь 100 г", "price": 900, "in_stock": True,
     "description": "Цветочный улун. Хорош по утрам.", "recommended": ["Шу Пуэр 100 г", "Да Хун Пао 100 г"]},
    {"name": "Шу Пуэр 100 г", "price": 700, "in_stock": False, "description": "Тёмный.", "recommended": []},
    {"name": "Да Хун Пао 100 г", "price": 1200, "in_stock": True,
     "description": "Утёсный улун с жареными нотами. Для вечера.", "recommended": []},
    {"name": "Габа 50 г", "price": 450, "in_stock": True, "description": "", "recommended": []},
]


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(catalog_service, "load_items", lambda: [dict(i) for i in CATALOG])
    monkeypatch.setattr(settings, "free_delivery_threshold", "")
    return CATALOG


def test_sheet_validates_recommended_names():
    good = "Название,Цена,С чем советуем\nУлун,500,пуэр\nПуэр,600,\n"
    parsed = sheet.parse_csv(good)
    assert parsed.errors == []
    assert parsed.items[0]["recommended"] == ["Пуэр"]

    bad = "Название,Цена,С чем советуем\nУлун,500,\"Пуэр, Габа\"\nПуэр,600,\n"
    parsed = sheet.parse_csv(bad)
    assert any("«Улун»" in e and "Габа" in e for e in parsed.errors)


def test_upsell_picks_first_in_stock_not_in_draft(catalog):
    draft_items = [{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 900}]
    # Шу Пуэр не в наличии — берём следующий.
    assert catalog_service.upsell_for(draft_items)["name"] == "Да Хун Пао 100 г"
    # Уже в черновике — не предлагаем.
    both = draft_items + [{"name": "Да Хун Пао 100 г", "quantity": 1, "price": 1200}]
    assert catalog_service.upsell_for(both) is None
    # Пустой столбец — ничего.
    assert catalog_service.upsell_for([{"name": "Габа 50 г", "quantity": 1, "price": 450}]) is None


async def test_upsell_is_offered_once_per_draft(clean, catalog):
    result = await conversation._execute_propose_order(
        PEER, {"items": [{"name": "Те Гуань Инь 100 г", "quantity": 1}]}
    )
    assert "Предложи дополнить: Да Хун Пао 100 г" in result
    assert "Утёсный улун с жареными нотами." in result
    draft = await state.get_draft(PEER)
    assert draft.details["upsell_offered"] is True

    added = await conversation._execute_add_to_order(
        PEER, {"items": [{"name": "Да Хун Пао 100 г", "quantity": 1}]}
    )
    assert "Добавлено: Да Хун Пао 100 г × 1" in added and "Предложи дополнить" not in added
    draft = await state.get_draft(PEER)
    assert draft.items_total == 2100 and draft.details["upsell_offered"] is True
    assert "Предложи дополнить" not in conversation._describe_draft(draft)
    await state.clear_draft(PEER)


async def test_add_to_order_resets_counted_delivery(clean, catalog):
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 900}], items_total=900,
        delivery_method="ozon_pvz", delivery_label="Ozon, пункт выдачи: Краснодар, Ставропольская, 230",
        delivery_cost=117, stage="awaiting_confirmation",
        details={"address": "Краснодар", "ozon_point_id": 1,
                 "ozon_point_address": "Краснодар, Ставропольская, 230"},
    ))
    result = await conversation._execute_add_to_order(
        PEER, {"items": [{"name": "Габа 50 г", "quantity": 2}]}
    )
    draft = await state.get_draft(PEER)
    assert draft.stage == "awaiting_delivery" and draft.delivery_cost is None
    assert "ozon_point_id" not in draft.details
    assert "method=ozon_pvz" in result and "Ставропольская, 230" in result
    await state.clear_draft(PEER)


def test_free_delivery_over_threshold(monkeypatch):
    monkeypatch.setattr(settings, "free_delivery_threshold", "2000")
    draft = OrderDraft(items=[{"name": "Да Хун Пао 100 г", "quantity": 2, "price": 1200}],
                       items_total=2400, delivery_cost=317)
    note = conversation._apply_free_delivery(draft)
    assert draft.delivery_cost == 0 and draft.details["carrier_delivery_cost"] == 317
    assert "бесплатная" in note

    # Чек: позиции доставки нет, сумма чека — сумма товаров.
    rows = yookassa_client.receipt_items(draft.items, draft.delivery_cost, "Ozon")
    assert all(not row["description"].startswith("Доставка") for row in rows)
    assert yookassa_client.receipt_total(rows) == 2400


async def test_payment_amount_matches_receipt_without_delivery(monkeypatch):
    sent = {}

    async def fake_call(method, path, payload, key=None):
        sent["payload"] = payload
        return {"id": "p1", "status": "pending", "paid": False,
                "amount": payload["amount"], "confirmation": {"confirmation_url": "https://x"}}

    monkeypatch.setattr(yookassa_client, "_call", fake_call)
    await yookassa_client.create_payment(
        order_key="vk1-1", items=[{"name": "Да Хун Пао", "quantity": 2, "price": 1200}],
        delivery_cost=0, delivery_label="Ozon", email="a@b.ru", phone="+79181234567",
        full_name="Иванов Иван", description="Заказ",
    )
    payload = sent["payload"]
    items_sum = sum(float(r["amount"]["value"]) * r["quantity"] for r in payload["receipt"]["items"])
    assert float(payload["amount"]["value"]) == items_sum == 2400
    assert len(payload["receipt"]["items"]) == 1


def test_below_threshold_draft_says_how_much_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "free_delivery_threshold", "2000")
    draft = OrderDraft(items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 900}],
                       items_total=900, delivery_cost=117, stage="awaiting_confirmation")
    assert conversation._apply_free_delivery(draft) == "" and draft.delivery_cost == 117
    assert "До бесплатной доставки не хватает 1100 ₽." in conversation._describe_draft(draft)


def test_without_threshold_nothing_is_said(monkeypatch):
    monkeypatch.setattr(settings, "free_delivery_threshold", "")
    draft = OrderDraft(items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 900}],
                       items_total=900, delivery_cost=117, stage="awaiting_confirmation")
    assert conversation._apply_free_delivery(draft) == "" and draft.delivery_cost == 117
    assert "бесплатн" not in conversation._describe_draft(draft)
