"""Сборка со сканированием: ссылка, проверки скана, «Собрано»."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import settings
from app.modules.catalog import service as catalog_service
from app.modules.marking import packing, pool
from app.modules.marking.codes import GS
from app.modules.marking.models import SOLD, MarkingCodeRow
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order

GTIN_TEA = "04606203099221"
GTIN_OTHER = "04650117240408"


def code(serial: str, gtin: str = GTIN_TEA) -> str:
    return f"01{gtin}21{serial}{GS}93dGVz"


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    items = [
        {"name": "Те Гуань Инь", "price": 800, "gtin": GTIN_TEA},
        {"name": "Да Хун Пао", "price": 1100, "gtin": ""},
    ]
    monkeypatch.setattr(catalog_service, "load_items", lambda: items)
    monkeypatch.setattr(settings, "internal_api_token", "token-for-tests")
    monkeypatch.setattr(settings, "public_base_url", "https://bba.containers.yandexcloud.net")
    return items


async def make_order(db, items=None, **fields) -> Order:
    values = dict(
        peer_id=8200, items=items or [{"name": "Те Гуань Инь", "quantity": 2, "price": 800}],
        items_total=1600, delivery_cost=117, total=1717, delivery_method="ozon_pvz",
        status="confirmed", payment_status=orders_repository.PAID, payment_id="pay-1",
        ozon_posting="0001-1", details={"recipient_email": "a@b.ru"},
        created_at=datetime.now(timezone.utc),
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.commit()
        return order


def test_link_signature_and_expiry():
    token, expires = packing.make_token(12)
    assert packing.read_token(token) == 12
    with pytest.raises(packing.LinkError, match="истёк"):
        packing.read_token(token, now=expires + timedelta(seconds=1))
    forged = token.replace("12.", "13.", 1)
    with pytest.raises(packing.LinkError, match="не подходит"):
        packing.read_token(forged)
    with pytest.raises(packing.LinkError, match="повреждена"):
        packing.read_token("мусор")


async def test_card_has_link_only_with_gtin(clean, catalog, monkeypatch):
    order = await make_order(clean)
    assert "/pack/" in packing.card_line(order)
    monkeypatch.setattr(catalog_service, "load_items", lambda: [{"name": "Чай", "gtin": ""}])
    assert packing.card_line(order) == ""


async def test_happy_path(clean):
    order = await make_order(clean)
    first = await packing.scan(order.id, code("AAAAAAAAAAAA1"), by="Оля")
    assert first.ok and first.message.endswith("1 из 2")
    assert "не из пула" in first.warning
    assert not first.state.can_finish

    with pytest.raises(packing.LinkError, match="не на все пачки"):
        await packing.finish(order.id)

    second = await packing.scan(order.id, code("AAAAAAAAAAAA2"), by="Оля")
    assert second.ok and second.state.can_finish
    done = await packing.finish(order.id, by="Оля")
    assert done.packed_at is not None
    assert await packing.count_assigned(order.id) == 2

    # После «Собрано» коды закреплены.
    late = await packing.scan(order.id, code("AAAAAAAAAAAA3"))
    assert not late.ok and "уже собран" in late.message
    row = (await packing.codes_of(order.id))[0]
    with pytest.raises(packing.LinkError, match="уже собран"):
        await packing.remove(order.id, row.id)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4606203099221", "не код маркировки"),
        (f"01{GTIN_TEA}21SERIAL", "криптохвоста"),
        (code("BBBBBBBBBBBB1", GTIN_OTHER), "другого сорта"),
    ],
)
async def test_bad_scans(clean, raw, expected):
    order = await make_order(clean)
    result = await packing.scan(order.id, raw)
    assert not result.ok and expected in result.message


async def test_no_more_codes_than_packs(clean):
    order = await make_order(clean, items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}])
    assert (await packing.scan(order.id, code("CCCCCCCCCCCC1"))).ok
    again = await packing.scan(order.id, code("CCCCCCCCCCCC1"))
    assert not again.ok and "уже отсканирован в этом заказе" in again.message
    extra = await packing.scan(order.id, code("CCCCCCCCCCCC2"))
    assert not extra.ok and "все 1 пачка" in extra.message


async def test_code_cannot_serve_two_orders(clean):
    first = await make_order(clean)
    second = await make_order(clean)
    assert (await packing.scan(first.id, code("DDDDDDDDDDDD1"))).ok
    stolen = await packing.scan(second.id, code("DDDDDDDDDDDD1"))
    assert not stolen.ok and f"заказу №{first.id}" in stolen.message
    # Тот же код без разделителей — та же пачка.
    sneaky = await packing.scan(second.id, code("DDDDDDDDDDDD1").replace(GS, ""), manual=True)
    assert not sneaky.ok and f"заказу №{first.id}" in sneaky.message


async def test_sold_code_is_refused(clean):
    order = await make_order(clean)
    async with clean() as session:
        session.add(MarkingCodeRow(code=code("EEEEEEEEEEEE1"), gtin=GTIN_TEA,
                                   serial="EEEEEEEEEEEE1", status=SOLD, from_pool=False))
        await session.commit()
    result = await packing.scan(order.id, code("EEEEEEEEEEEE1"))
    assert not result.ok and "уже продан" in result.message


async def test_pool_is_enforced_once_imported(clean):
    await pool.import_codes(code("FFFFFFFFFFFF1") + "\n")
    order = await make_order(clean)
    outsider = await packing.scan(order.id, code("FFFFFFFFFFFF9"))
    assert not outsider.ok and "нет в пуле" in outsider.message
    insider = await packing.scan(order.id, code("FFFFFFFFFFFF1"))
    assert insider.ok and insider.warning == ""


async def test_position_without_gtin_blocks_finish(clean):
    order = await make_order(clean, items=[
        {"name": "Те Гуань Инь", "quantity": 1, "price": 800},
        {"name": "Да Хун Пао", "quantity": 1, "price": 1100},
    ])
    assert (await packing.scan(order.id, code("GGGGGGGGGGGG1"))).ok
    current = await packing.state(order.id)
    assert not current.can_finish
    assert current.as_dict()["positions"][1]["gtin"] == ""
    with pytest.raises(packing.LinkError, match="нет GTIN"):
        await packing.finish(order.id)


async def test_remove_returns_code(clean):
    await pool.import_codes(code("HHHHHHHHHHHH1") + "\n")
    order = await make_order(clean)
    scanned = await packing.scan(order.id, code("HHHHHHHHHHHH1"))
    code_id = scanned.state.positions[0].codes[0]["id"]
    after = await packing.remove(order.id, code_id)
    assert after.positions[0].codes == []
    # Код из пула остался в пуле и годится снова.
    assert (await packing.scan(order.id, code("HHHHHHHHHHHH1"))).ok


async def test_refunded_order_is_closed(clean):
    order = await make_order(clean, status="refunded")
    result = await packing.scan(order.id, code("IIIIIIIIIIII1"))
    assert not result.ok and "возврат" in result.message


async def test_page_over_http(clean):
    from app.main import app

    order = await make_order(clean, items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}])
    token, _ = packing.make_token(order.id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        page = await http.get(f"/pack/{token}")
        wasm = await http.get("/pack/static/zxing_reader.wasm")
        bad = await http.post("/pack/1.1.xxx/state")
        scanned = await http.post(f"/pack/{token}/scan", json={"code": code("JJJJJJJJJJJJ1"), "by": "Оля"})
        finished = await http.post(f"/pack/{token}/finish", json={"by": "Оля"})
    assert page.status_code == 200 and "Сборка заказа" in page.text
    assert page.headers["referrer-policy"] == "no-referrer"
    assert wasm.headers["content-type"] == "application/wasm"
    assert bad.status_code == 403
    assert scanned.json()["ok"] is True
    assert finished.json()["state"]["packed_at"]
