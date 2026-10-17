"""Сеты и наборы при сборке, строки закрывающего чека и полный возврат с кодами.

Снимки JSON лежат в `tests/snapshots/settlement/`. Поменяли состав чека
намеренно — пересобрать снимки: `UPDATE_SNAPSHOTS=1 pytest -k snapshot`.
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import settings
from app.modules.catalog import service as catalog_service
from app.modules.marking import packing
from app.modules.marking.codes import GS
from app.modules.marking.models import PACK, RETURNED, SET, SOLD, OrderMarkingCode
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order, OrderPayment
from app.modules.payment import returns, settlement, webhook, yookassa_client

GTIN_TGY = "04606203099221"    # пачка Те Гуань Инь 100 г
GTIN_SET = "04650117240408"    # сет «Два улуна»: 2 пачки по 100 г в коробке
GTIN_MINI = "04603934000793"   # пачка 35 г — бывает только внутри набора

_PAYMENTS = itertools.count(1)
SNAPSHOTS = Path(__file__).parent / "snapshots" / "settlement"

CATALOG = [
    {"name": "Те Гуань Инь", "price": 800, "gtin": GTIN_TGY, "kind": PACK},
    {"name": "Сет «Два улуна»", "price": 1500, "gtin": GTIN_SET, "kind": SET},
    {"name": "Те Гуань Инь 35 г", "price": 300, "gtin": GTIN_MINI, "kind": PACK},
    {"name": "Да Хун Пао", "price": 1100, "gtin": "", "kind": PACK},
]


def code(serial: str, gtin: str = GTIN_TGY) -> str:
    return f"01{gtin}21{serial}{GS}93dGVz"


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(catalog_service, "load_items", lambda: CATALOG)
    monkeypatch.setattr(settings, "yookassa_vat_code", 1)
    monkeypatch.setattr(settings, "yookassa_receipt_timezone", 2)


def assert_snapshot(name: str, data: dict) -> None:
    path = SNAPSHOTS / f"{name}.json"
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"нет снимка {path.name} — запустите с UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(encoding="utf-8")


# --- строки чека: без базы ------------------------------------------------------


def order_of(items: list[dict], delivery_cost: float = 117) -> Order:
    items_total = sum(i["price"] * i["quantity"] for i in items)
    return Order(
        id=17, peer_id=1, items=items, items_total=items_total, delivery_cost=delivery_cost,
        total=items_total + delivery_cost, delivery_method="ozon_pvz", payment_id="pay-17",
        details={"recipient_email": "a@b.ru", "recipient_name": "Иванов Иван",
                 "delivery_label": "Ozon, пункт выдачи"},
    )


def codes_for(*positions: tuple[int, str, str, list[str]]) -> list[OrderMarkingCode]:
    """(номер позиции, GTIN, тип, серийники) → строки кодов заказа."""
    return [
        OrderMarkingCode(item_index=index, raw_code=code(serial, gtin), gtin=gtin,
                         serial=serial, kind=kind, status=SOLD)
        for index, gtin, kind, serials in positions
        for serial in serials
    ]


def prepayment_total(order: Order) -> float:
    goods, delivery = settlement._prepayment_rows(order)
    return yookassa_client.receipt_total(goods + delivery)


def test_split_kopecks_puts_the_rest_on_the_last_line():
    assert settlement.split_kopecks(100000, 3) == [33333, 33333, 33334]
    assert settlement.split_kopecks(160000, 2) == [80000, 80000]
    assert settlement.split_kopecks(1, 1) == [1]
    assert sum(settlement.split_kopecks(99999, 7)) == 99999
    with pytest.raises(ValueError):
        settlement.split_kopecks(100, 0)


def test_snapshot_one_pack():
    order = order_of([{"name": "Те Гуань Инь", "quantity": 1, "price": 800}])
    body = settlement.build_payload(order, codes_for((0, GTIN_TGY, PACK, ["TGY0000000001"])))
    assert_snapshot("one_pack", body)
    assert yookassa_client.receipt_total(body["items"]) == prepayment_total(order) == 917.0


def test_snapshot_two_identical_packs():
    order = order_of([{"name": "Те Гуань Инь", "quantity": 2, "price": 800}])
    body = settlement.build_payload(
        order, codes_for((0, GTIN_TGY, PACK, ["TGY0000000001", "TGY0000000002"]))
    )
    assert_snapshot("two_identical_packs", body)
    packs = [i for i in body["items"] if i["payment_subject"] == "marked"]
    # Две одинаковые пачки — две строки по одной, у каждой свой код.
    assert [i["quantity"] for i in packs] == [1, 1]
    assert len({i["mark_code_info"]["gs_1m"] for i in packs}) == 2


def test_snapshot_set():
    order = order_of([{"name": "Сет «Два улуна»", "quantity": 1, "price": 1500}])
    body = settlement.build_payload(order, codes_for((0, GTIN_SET, SET, ["SET0000000001"])))
    assert_snapshot("set", body)
    # Сет — одна строка с КИН с коробки; пачки внутри в чек не идут.
    marked = [i for i in body["items"] if i["payment_subject"] == "marked"]
    assert len(marked) == 1 and marked[0]["mark_code_info"]["gs_1m"] == code("SET0000000001", GTIN_SET)


def test_snapshot_mixed_set_and_pack():
    order = order_of([
        {"name": "Сет «Два улуна»", "quantity": 1, "price": 1500},
        {"name": "Те Гуань Инь", "quantity": 2, "price": 800},
    ])
    body = settlement.build_payload(order, codes_for(
        (0, GTIN_SET, SET, ["SET0000000001"]),
        (1, GTIN_TGY, PACK, ["TGY0000000001", "TGY0000000002"]),
    ))
    assert_snapshot("mixed_set_and_pack", body)
    assert yookassa_client.receipt_total(body["items"]) == prepayment_total(order) == 3217.0
    assert body["settlements"][0]["amount"]["value"] == "3217.00"


def test_split_price_matches_the_prepayment_to_the_kopeck():
    """Нецелая цена × три пачки: строки делятся, итог сходится с чеком предоплаты."""
    order = order_of([{"name": "Те Гуань Инь", "quantity": 3, "price": 333.33}], delivery_cost=125.24)
    body = settlement.build_payload(
        order, codes_for((0, GTIN_TGY, PACK, ["TGY01", "TGY02", "TGY03"]))
    )
    lines = [i["amount"]["value"] for i in body["items"] if i["payment_subject"] == "marked"]
    assert lines == ["333.33", "333.33", "333.33"]
    assert yookassa_client.receipt_total(body["items"]) == prepayment_total(order) == 1125.23
    assert body["settlements"][0]["amount"]["value"] == "1125.23"


def test_codes_must_cover_every_unit():
    order = order_of([{"name": "Те Гуань Инь", "quantity": 2, "price": 800}])
    with pytest.raises(settlement.NotReady, match="кодов 1, а единиц 2"):
        settlement.build_payload(order, codes_for((0, GTIN_TGY, PACK, ["TGY0000000001"])))


def test_snapshot_full_return_receipt():
    order = order_of([
        {"name": "Сет «Два улуна»", "quantity": 1, "price": 1500},
        {"name": "Те Гуань Инь", "quantity": 1, "price": 800},
    ])
    receipt = settlement.return_receipt(order, codes_for(
        (0, GTIN_SET, SET, ["SET0000000001"]),
        (1, GTIN_TGY, PACK, ["TGY0000000001"]),
    ))
    assert_snapshot("full_return", receipt)
    for item in receipt["items"]:
        assert item["payment_mode"] == "full_payment"
    assert yookassa_client.receipt_total(receipt["items"]) == prepayment_total(order)


# --- сборка с сетами: с базой ---------------------------------------------------


async def make_order(db, items, **fields) -> Order:
    items_total = sum(i["price"] * i["quantity"] for i in items)
    values = dict(
        peer_id=8500, items=items, items_total=items_total, delivery_cost=117,
        total=items_total + 117, delivery_method="ozon_pvz", status="confirmed",
        payment_status=orders_repository.PAID, payment_id=f"pay-85{next(_PAYMENTS)}",
        details={"recipient_email": "a@b.ru", "recipient_name": "Иванов Иван"},
        created_at=datetime.now(timezone.utc),
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.flush()
        session.add(OrderPayment(payment_id=order.payment_id, order_id=order.id, attempt=1,
                                 status="succeeded", amount=values["total"]))
        await session.commit()
        return order


SET_ITEM = {"name": "Сет «Два улуна»", "quantity": 1, "price": 1500}
PACK_ITEM = {"name": "Те Гуань Инь", "quantity": 2, "price": 800}


async def test_set_takes_one_box_code_and_not_the_packs_inside(clean):
    order = await make_order(clean, [SET_ITEM])
    inner = await packing.scan(order.id, code("MINI00000001", GTIN_MINI))
    assert not inner.ok
    assert "Это не та пачка: в заказе «Сет «Два улуна»», отсканировано «Те Гуань Инь 35 г»" in inner.message
    assert "Пачки внутри сета не сканируются" in inner.message

    box = await packing.scan(order.id, code("SET000000001", GTIN_SET))
    assert box.ok and box.state.can_finish
    extra = await packing.scan(order.id, code("SET000000002", GTIN_SET))
    assert not extra.ok and "все 1 сет" in extra.message

    done = await packing.finish(order.id)
    assert done.packed_at is not None
    [row] = await packing.codes_of(order.id)
    assert row.kind == SET and row.gtin == GTIN_SET


async def test_mixed_order_is_packed_only_when_every_position_is_closed(clean):
    order = await make_order(clean, [SET_ITEM, PACK_ITEM])
    assert (await packing.scan(order.id, code("SET000000001", GTIN_SET))).ok
    assert (await packing.scan(order.id, code("TGY000000001"))).ok
    with pytest.raises(packing.LinkError, match="Те Гуань Инь: 1 из 2"):
        await packing.finish(order.id)
    assert (await packing.scan(order.id, code("TGY000000002"))).ok
    await packing.finish(order.id)
    rows = await packing.codes_of(order.id)
    assert [(r.item_index, r.kind) for r in rows] == [(0, SET), (1, PACK), (1, PACK)]


async def test_scan_without_gs_is_stored_with_gs(clean):
    order = await make_order(clean, [{"name": "Те Гуань Инь", "quantity": 1, "price": 800}])
    result = await packing.scan(order.id, code("TGY000000009").replace(GS, ""))
    assert result.ok and "разделитель GS восстановлен" in result.warning
    [row] = await packing.codes_of(order.id)
    assert row.raw_code == code("TGY000000009") and GS in row.raw_code and row.manual


async def test_position_without_gtin_says_so(clean):
    order = await make_order(clean, [{"name": "Да Хун Пао", "quantity": 1, "price": 1100}])
    result = await packing.scan(order.id, code("TGY000000001"))
    assert not result.ok and "Нет GTIN в каталоге у «Да Хун Пао»" in result.message
    assert result.state.as_dict()["positions"][0]["gtin"] == ""


# --- полный возврат с кодами ------------------------------------------------------


@pytest.fixture
def money(monkeypatch):
    box = {"calls": [], "manager": [], "client": [], "refund_status": "succeeded", "refunds": {}}

    async def fake_call(method, path, payload=None, key=""):
        box["calls"].append((method, path, payload, key))
        refund = {"id": f"ref-{len(box['calls'])}", "payment_id": payload["payment_id"],
                  "status": box["refund_status"], "amount": payload["amount"]}
        box["refunds"][refund["id"]] = refund
        return refund

    async def get_refund(refund_id):
        data = box["refunds"][refund_id]
        return yookassa_client.Refund(id=refund_id, payment_id=data["payment_id"],
                                      status="succeeded", amount=float(data["amount"]["value"]))

    async def to_manager(order, text):
        box["manager"].append(text)

    async def to_client(peer_id, text, random_id=None):
        box["client"].append(text)

    monkeypatch.setattr(yookassa_client, "_call", fake_call)
    monkeypatch.setattr(webhook.yookassa_client, "get_refund", get_refund)
    monkeypatch.setattr(webhook.order_chat, "send", to_manager)
    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    return box


async def sold_order(db, items=(SET_ITEM, PACK_ITEM)) -> Order:
    """Вручённый заказ с зарегистрированным закрывающим чеком — коды проданы."""
    order = await make_order(db, list(items))
    serials = {GTIN_SET: iter(["SET000000001", "SET000000002"]),
               GTIN_TGY: iter(["TGY000000001", "TGY000000002", "TGY000000003"])}
    for item in items:
        gtin = catalog_service.gtin_for(item["name"])
        for _ in range(item["quantity"]):
            assert (await packing.scan(order.id, code(next(serials[gtin]), gtin))).ok
    await packing.finish(order.id)
    assert await packing.mark_sold(order.id) == sum(i["quantity"] for i in items)
    await orders_repository.set_state(
        order.id, delivered_at=datetime.now(timezone.utc),
        settlement_receipt_id="rt-1", settlement_receipt_status="succeeded",
    )
    return await orders_repository.by_id(order.id)


async def code_statuses(order_id: int) -> set[str]:
    return {row.status for row in await packing.codes_of(order_id, statuses=(SOLD, RETURNED))}


async def test_full_return_passes_the_same_codes_and_marks_them_returned(clean, money):
    order = await sold_order(clean)
    outcome = await returns.refund_with_codes(order)
    assert outcome["статус"] == "succeeded" and outcome["кодов в чеке возврата"] == 3

    [(method, path, payload, key)] = money["calls"]
    assert (method, path) == ("POST", "/refunds")
    assert key == returns.idempotence_key(order.id)
    assert payload["amount"] == {"value": "3217.00", "currency": "RUB"}
    marked = [i for i in payload["receipt"]["items"] if i["payment_subject"] == "marked"]
    assert [i["mark_code_info"]["gs_1m"] for i in marked] == [
        code("SET000000001", GTIN_SET), code("TGY000000001"), code("TGY000000002"),
    ]
    assert all(i["payment_mode"] == "full_payment" for i in payload["receipt"]["items"])
    assert payload["receipt"]["timezone"] == 2
    assert await code_statuses(order.id) == {RETURNED}

    # Уведомление ЮKassa о том же возврате: заказ возвращён, коды не трогаются дважды.
    await webhook.handle({"event": "refund.succeeded", "object": {"id": outcome["возврат"]}})
    fresh = await orders_repository.by_id(order.id)
    assert fresh.status == "refunded"
    assert any("ушли в чек возврата: 3 шт." in text for text in money["manager"])
    assert any("Оформили возврат" in text for text in money["client"])

    # Вернувшийся код снова в обороте — эту пачку можно продать новым заказом.
    again = await make_order(clean, [{"name": "Те Гуань Инь", "quantity": 1, "price": 800}])
    assert (await packing.scan(again.id, code("TGY000000001"))).ok


async def test_webhook_before_our_answer_still_returns_the_codes(clean, money):
    """Уведомление обогнало ответ на создание возврата: опознаём по отметке и сумме."""
    order = await sold_order(clean)
    money["refund_status"] = "pending"
    outcome = await returns.refund_with_codes(order)
    assert outcome["отмечено возвращёнными"] == 0
    await orders_repository.set_state(order.id, details={
        **order.details, "codes_return": {"requested_at": "2026-10-16T10:00:00+00:00"},
    })
    await webhook.handle({"event": "refund.succeeded", "object": {"id": outcome["возврат"]}})
    assert await code_statuses(order.id) == {RETURNED}
    assert any("ушли в чек возврата: 3 шт." in text for text in money["manager"])


async def test_return_with_codes_needs_a_settlement_receipt(clean, money):
    order = await make_order(clean, [PACK_ITEM])
    outcome = await returns.refund_with_codes(order)
    assert outcome["возврат"] == "не сделан" and "кнопкой в кабинете" in outcome["почему"]
    assert money["calls"] == []


async def test_cabinet_refund_after_settlement_is_flagged(clean, money):
    order = await sold_order(clean, items=(PACK_ITEM,))
    money["refunds"]["ref-cab"] = {"payment_id": order.payment_id,
                                   "amount": {"value": "1717.00", "currency": "RUB"}}
    await webhook.handle({"event": "refund.succeeded", "object": {"id": "ref-cab"}})
    assert any(f"orders/{order.id}/refund-with-codes" in text for text in money["manager"])
    assert await code_statuses(order.id) == {SOLD}


async def test_partial_return_from_a_set_goes_to_the_manager(clean, money):
    """TODO(chz-partial-return): одну пачку из сета бот не возвращает — только менеджеру."""
    order = await sold_order(clean)
    money["refunds"]["ref-part"] = {"payment_id": order.payment_id,
                                    "amount": {"value": "750.00", "currency": "RUB"}}
    await webhook.handle({"event": "refund.succeeded", "object": {"id": "ref-part"}})
    assert any("частичный возврат по заказу с сетом" in text for text in money["manager"])
    assert await code_statuses(order.id) == {SOLD}
