"""Funnel v3, задача 3: способ оплаты выбирает клиент, yookassa/me."""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.modules.payment import yookassa_client


async def test_payment_is_redirect_without_a_fixed_method(monkeypatch):
    sent = {}

    async def call(method, path, payload=None, key=""):
        sent.update(method=method, path=path, payload=payload)
        return {"id": "p1", "status": "pending", "amount": {"value": "1617.00"},
                "confirmation": {"confirmation_url": "https://yoomoney.ru/checkout/p1"}}

    monkeypatch.setattr(yookassa_client, "_call", call)
    await yookassa_client.create_payment(
        order_key="vk1-1", items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 1500}],
        delivery_cost=117, delivery_label="Ozon", email="a@b.ru", phone="+79001234567",
        full_name="Иванов Иван", description="Заказ",
    )
    payload = sent["payload"]
    assert sent["path"] == "/payments"
    assert payload["confirmation"]["type"] == "redirect"
    assert "payment_method_data" not in payload and "payment_method_id" not in payload


async def test_yookassa_me_reports_only_what_the_api_gave(monkeypatch):
    from app.main import app

    monkeypatch.setattr(settings, "internal_api_token", "t0ken")
    monkeypatch.setattr(yookassa_client, "is_configured", lambda: True)

    async def me():
        return {"account_id": "1475067", "test": False, "status": "enabled",
                "fiscalization": {"enabled": True, "provider": "avanpost"},
                "payment_methods": ["bank_card", "yoo_money", "sbp"]}

    monkeypatch.setattr(yookassa_client, "account_info", me)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        full = (await http.post("/internal/yookassa/me", content=b"t0ken")).json()

        async def bare():
            return {"account_id": "1475067", "test": False}

        monkeypatch.setattr(yookassa_client, "account_info", bare)
        thin = (await http.post("/internal/yookassa/me", content=b"t0ken")).json()
    assert full["контур"] == "БОЕВОЙ" and full["способы оплаты"] == ["bank_card", "yoo_money", "sbp"]
    assert full["фискализация"] == {"enabled": True, "provider": "avanpost"}
    assert thin["способы оплаты"] == thin["фискализация"] == "ЮKassa в ответе /me это поле не отдала"
