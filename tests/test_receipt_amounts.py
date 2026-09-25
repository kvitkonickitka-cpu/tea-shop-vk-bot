"""Сумма чека сходится с суммой платежа, сколько бы пачек ни было."""

from __future__ import annotations

from app.modules.payment import yookassa_client


def test_item_amount_is_unit_price():
    rows = yookassa_client.receipt_items(
        [{"name": "Те Гуань Инь", "quantity": 2, "price": 800}], 117, "Ozon"
    )
    assert rows[0]["amount"] == {"value": "800.00", "currency": "RUB"}
    assert rows[0]["quantity"] == 2
    assert rows[1]["amount"] == {"value": "117.00", "currency": "RUB"}
    assert yookassa_client.receipt_total(rows) == 1717.0


async def test_payment_amount_matches_receipt(monkeypatch):
    sent: dict = {}

    async def fake_call(method, path, payload=None, key=""):
        sent.update(payload)
        return {"id": "p1", "status": "pending", "amount": payload["amount"]}

    monkeypatch.setattr(yookassa_client, "_call", fake_call)
    await yookassa_client.create_payment(
        order_key="vk1-1",
        items=[
            {"name": "Те Гуань Инь", "quantity": 3, "price": 800},
            {"name": "Да Хун Пао", "quantity": 1, "price": 1100.5},
        ],
        delivery_cost=117, delivery_label="Ozon", email="a@b.ru", phone="",
        full_name="Иванов", description="Заказ",
    )
    items = sent["receipt"]["items"]
    by_receipt = sum(float(i["amount"]["value"]) * i["quantity"] for i in items)
    assert sent["amount"]["value"] == "3617.50"
    assert round(by_receipt, 2) == 3617.50
