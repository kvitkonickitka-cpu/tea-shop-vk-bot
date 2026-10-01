"""Цена Ozon считается со служебным телефоном, а не с номером получателя."""

from __future__ import annotations

from app.modules.orders import conversation
from app.modules.orders.state import OrderDraft


async def test_quote_ignores_recipient_phone(monkeypatch):
    seen = {}

    async def price_for(point_id, *, phone, weight_grams, declared_value):
        seen["phone"] = phone
        return "quote"

    monkeypatch.setattr(conversation.ozon_quote, "price_for", price_for)
    draft = OrderDraft(
        items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 1500}], items_total=1500,
        # Так номер мог сохраниться в старом заказе — Ozon его отвергал.
        details={"recipient_phone": "8 (921) 447-76-22"},
    )
    assert await conversation._ozon_price(draft, 46348) == "quote"
    assert seen["phone"] == ""
