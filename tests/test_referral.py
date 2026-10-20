"""Реферальная программа (пакет D): чек со скидкой, коды, проверки, резерв, сигналы, награда."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import flags
from app.core.config import settings
from app.messages import funnel
from app.messages.models import FunnelEvent
from app.modules.analytics import service as analytics
from app.modules.orders import state
from app.modules.orders.models import Order
from app.modules.orders.state import OrderDraft
from app.modules.payment import yookassa_client
from app.modules.referral import pricing, service as referral
from app.modules.referral.models import ReferralRedemption, ReferralReward

OWNER, FRIEND, FRIEND2, STRANGER = 9981, 9982, 9983, 9984


# --- чек: скидка по позициям до копеек ------------------------------------------------


def _total(items) -> Decimal:
    return sum(Decimal(str(i["price"])) * int(i["quantity"]) for i in items)


@pytest.mark.parametrize("items, discount", [
    ([{"name": "A", "price": 1500, "quantity": 1}], 15000),
    ([{"name": "A", "price": 1500, "quantity": 3}, {"name": "B", "price": 990, "quantity": 1}], 50000),
    ([{"name": "A", "price": 333.33, "quantity": 3}, {"name": "B", "price": 1001, "quantity": 2},
      {"name": "C", "price": 12.5, "quantity": 7}], 12345),
    ([{"name": "A", "price": 100, "quantity": 7}], 1),
])
def test_discount_is_spread_to_the_cent(items, discount):
    out = pricing.distribute(items, discount)
    assert _total(out) == _total(items) - Decimal(discount) / 100
    # Ни одной цены с долями копейки, у каждой позиции — её прайс.
    assert all(Decimal(str(i["price"])).as_tuple().exponent >= -2 for i in out)
    assert all(i["list_price"] == next(x["price"] for x in items if x["name"] == i["name"]) for i in out)
    # Количество штук по каждому товару не меняется (строка могла поделиться на две).
    for item in items:
        assert sum(i["quantity"] for i in out if i["name"] == item["name"]) == item["quantity"]
    # Обратно — те же позиции по прайсу.
    assert pricing.merge(out) == [{**i} for i in items]


def test_remainder_goes_to_the_last_line_and_codes_stay_with_items():
    items = [{"name": "A", "price": 1000, "quantity": 1, "gtin": "0469"},
             {"name": "B", "price": 1000, "quantity": 1, "gtin": "0470"}]
    out = pricing.distribute(items, 1)  # копейка — последней позиции
    assert [i["price"] for i in out] == [1000.0, 999.99] and [i["gtin"] for i in out] == ["0469", "0470"]


def test_receipt_sum_equals_payment_with_delivery_undiscounted():
    draft = OrderDraft(items=[{"name": "A", "price": 1500, "quantity": 2}, {"name": "B", "price": 990, "quantity": 1}],
                       delivery_cost=121.1, details={"referral": {"percent": 10, "cap": 500, "min_total": 1490}})
    pricing.reprice(draft)
    assert draft.details["referral"]["discount"] == 399.0 and draft.items_total == 3591.0
    rows = yookassa_client.receipt_items(draft.items, draft.delivery_cost, "Доставка")
    assert math.isclose(yookassa_client.receipt_total(rows), draft.items_total + 121.1)
    delivery = [r for r in rows if r.get("payment_subject") == "service"]
    assert delivery and float(delivery[0]["amount"]["value"]) == 121.1


def test_cap_and_minimum():
    draft = OrderDraft(items=[{"name": "A", "price": 2000, "quantity": 5}],
                       details={"referral": {"percent": 10, "cap": 500, "min_total": 1490}})
    pricing.reprice(draft)
    assert draft.details["referral"]["discount"] == 500.0 and draft.items_total == 9500.0
    draft.items = [{"name": "A", "price": 2000, "quantity": 5, "list_price": 2000}][:0] + [
        {"name": "B", "price": 1000, "quantity": 1}]
    pricing.reprice(draft)
    assert draft.details["referral"]["active"] is False and draft.items_total == 1000.0


# --- коды ----------------------------------------------------------------------------------


def test_code_format_and_entropy():
    codes = {referral.new_code() for _ in range(500)}
    assert len(codes) == 500
    for code in codes:
        assert re.fullmatch(r"ЧАЙ-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{4}-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{4}", code)
    assert math.log2(len(referral.ALPHABET) ** 8) >= 30
    assert not set("01OIL") & set(referral.ALPHABET)
    assert referral.normalize("чай-abcd efgh") == "ЧАЙ-ABCD-EFGH"
    assert referral.normalize("r_ABCDEFGH") == "ЧАЙ-ABCD-EFGH"
    assert referral.normalize("ЧАЙ-АВСD-ЕFGH") == "ЧАЙ-ABCD-EFGH"  # кириллица-двойники
    assert referral.normalize("ЧАЙ-0BCD-EFGH") is None
    assert referral.link_ref("ЧАЙ-ABCD-EFGH") == "r_ABCDEFGH"


@pytest.fixture
async def world(clean, monkeypatch):
    monkeypatch.setattr(settings, "test_vk_ids", f"{OWNER},{FRIEND},{FRIEND2}")
    monkeypatch.setattr(analytics, "_test_ids", None)
    monkeypatch.setattr(settings, "client_key_secret", "s3cret")
    await flags.set_mode(flags.REFERRAL, flags.ALLOWLIST, by="test")
    box = {"notify": []}

    async def notify(kind, text_, **kwargs):
        box["notify"].append(text_)
        return True

    monkeypatch.setattr("app.messages.manager.notify", notify)
    return box


async def paid_order(db, peer, *, delivered=True, phone="+79001112233", email="owner@mail.ru", name="Петров Пётр",
                     point=7, **fields) -> Order:
    now = datetime.now(timezone.utc)
    async with db() as session:
        order = Order(peer_id=peer, items=[{"name": "A", "quantity": 1, "price": 1500}], items_total=1500,
                      delivery_cost=0, total=1500, delivery_method="ozon_pvz", status="confirmed",
                      payment_status="succeeded", paid_at=now - timedelta(days=20),
                      delivered_at=now - timedelta(days=15) if delivered else None,
                      details={"recipient_phone": phone, "recipient_email": email, "recipient_name": name,
                               "ozon_point_id": point}, **fields)
        session.add(order)
        await session.commit()
        return order


async def friend_draft(peer, items=None, **details) -> OrderDraft:
    draft = OrderDraft(items=items or [{"name": "A", "price": 1500, "quantity": 2}], items_total=3000,
                       delivery_method="ozon_pvz", delivery_cost=121, stage="awaiting_confirmation",
                       details={"ozon_point_id": 9, **details})
    await state.set_draft(peer, draft)
    return await state.get_draft(peer)


async def events(db, peer) -> list[str]:
    async with db() as session:
        return list((await session.execute(select(FunnelEvent.event).where(FunnelEvent.peer_id == peer)
                                           .order_by(FunnelEvent.id))).scalars())


async def test_code_only_after_a_delivered_order(world, clean, monkeypatch):
    monkeypatch.setattr(settings, "test_vk_ids", "")
    monkeypatch.setattr(analytics, "_test_ids", None)
    assert await referral.code_for(STRANGER) is None
    await paid_order(clean, STRANGER, delivered=False)
    assert await referral.code_for(STRANGER) is None
    await paid_order(clean, STRANGER)
    first = await referral.code_for(STRANGER)
    assert first is not None and (await referral.code_for(STRANGER)).code == first.code
    assert "referral_code_issued" in await events(clean, STRANGER)


async def test_friend_gets_discount_in_draft_and_invoice(world, clean):
    code = await referral.code_for(OWNER)
    await friend_draft(FRIEND)
    ok, said = await referral.apply(FRIEND, code.code.lower())
    assert ok and "−300 ₽" in said
    draft = await state.get_draft(FRIEND)
    assert draft.items_total == 2700 and all(i["list_price"] == 1500 for i in draft.items)
    # Повторная запись черновика скидку не удваивает.
    await state.set_draft(FRIEND, draft)
    assert (await state.get_draft(FRIEND)).items_total == 2700
    assert "referral_code_applied" in await events(clean, FRIEND)


async def test_rejections(world, clean):
    code = await referral.code_for(OWNER)
    assert (await referral.apply(OWNER, code.code))[1] == referral.REJECT_TEXT["own_code"]
    await paid_order(clean, FRIEND2, phone="+79005550000", email="f2@mail.ru")
    assert (await referral.apply(FRIEND2, code.code))[1] == referral.REJECT_TEXT["not_first_order"]
    for _ in range(5):
        assert (await referral.apply(FRIEND, "ЧАЙ-AAAA-AAAA"))[1] == referral.REJECT_TEXT["not_found"]
    assert (await referral.apply(FRIEND, code.code))[1] == referral.REJECT_TEXT["blocked_today"]
    async with clean() as session:
        from app.modules.referral.models import ReferralAttempt

        assert len((await session.execute(select(ReferralAttempt))).scalars().all()) == 8


async def test_pending_code_applies_when_the_draft_appears(world, clean):
    code = await referral.code_for(OWNER)
    ok, said = await referral.apply(FRIEND, code.code)
    assert ok and "применится к заказу" in said
    draft = await friend_draft(FRIEND)
    line = await referral.auto_apply(FRIEND, draft)
    assert "Код друга применён" in line and (await state.get_draft(FRIEND)).items_total == 2700


async def test_same_person_is_caught_before_invoice(world, clean, monkeypatch):
    from app.modules.catalog import service as catalog_service
    from app.modules.orders import conversation

    monkeypatch.setattr(catalog_service, "load_items", lambda: [{"name": "A", "price": 1500, "in_stock": True}])

    code = await referral.code_for(OWNER)
    await paid_order(clean, OWNER)  # получатель владельца: +79001112233
    await friend_draft(FRIEND, recipient_name="Иван Иванов", recipient_phone="89001112233",
                       recipient_email="friend@mail.ru", seen_total=2821, quoted_at=9e12)
    await referral.apply(FRIEND, code.code)
    note = await conversation._refresh_before_invoice(FRIEND, await state.get_draft(FRIEND))
    assert "Скидка по коду друга снята" in note and referral.REJECT_TEXT["same_person"] in note
    draft = await state.get_draft(FRIEND)
    assert draft.items_total == 3000 and "referral" not in draft.details


async def test_reserve_apply_void_lifecycle(world, clean):
    code = await referral.code_for(OWNER)
    draft = await friend_draft(FRIEND, recipient_name="Иван", recipient_phone="89007770000", recipient_email="f@mail.ru")
    await referral.apply(FRIEND, code.code)
    draft = await state.get_draft(FRIEND)
    await referral.reserve(FRIEND, draft, order_id=501)
    await funnel.record(FRIEND, "invoice_expired", order_id=501)
    await referral.reserve(FRIEND, draft, order_id=501)
    await funnel.record(FRIEND, "payment_succeeded", order_id=501)
    async with clean() as session:
        rows = (await session.execute(select(ReferralRedemption.status).order_by(ReferralRedemption.id))).scalars().all()
    assert rows == ["voided", "applied"]
    assert "referral_redeemed" in await events(clean, FRIEND)
    # Скидку другу второй раз не дают — даже другим аккаунтом с тем же телефоном.
    other = await friend_draft(FRIEND2, recipient_name="Иван", recipient_phone="+79007770000", recipient_email="x@mail.ru")
    await referral.apply(FRIEND2, code.code)
    assert await referral.check_before_invoice(FRIEND2, await state.get_draft(FRIEND2)) is not None
    await funnel.record(FRIEND, "refunded", order_id=501)
    async with clean() as session:
        assert (await session.execute(select(ReferralRedemption.status).where(
            ReferralRedemption.order_id == 501, ReferralRedemption.status == "voided"))).scalars().all()
    del other


async def test_limits_and_abuse_signals(world, clean, monkeypatch):
    code = await referral.code_for(OWNER)
    for friend, phone in ((FRIEND, "89001230001"), (FRIEND2, "89001230002")):
        await friend_draft(friend, recipient_name=f"Друг {friend}", recipient_phone=phone,
                           recipient_email=f"{friend}@mail.ru", ozon_point_id=42)
        await referral.apply(friend, code.code)
        await referral.reserve(friend, await state.get_draft(friend), order_id=600 + friend)
    assert any("один пункт выдачи" in text for text in world["notify"])
    # Лимит кода — отказ и ⚠️ в Ops.
    async with clean() as session:
        stored = await session.get(type(code), code.id)
        stored.limit_total = 0
        await session.commit()
    monkeypatch.setattr(analytics, "_test_ids", {OWNER, FRIEND, FRIEND2, STRANGER})
    assert (await referral.apply(STRANGER, code.code))[1] == referral.REJECT_TEXT["limit_total"]
    assert any("лимит" in text for text in world["notify"])


async def test_reward_after_delivery_and_return_period(world, clean, monkeypatch):
    monkeypatch.setattr(settings, "referrer_reward", "percent:7")
    code = await referral.code_for(OWNER)
    order = await paid_order(clean, FRIEND, phone="+79009990000", email="fr@mail.ru")
    async with clean() as session:
        session.add(ReferralRedemption(code_id=code.id, friend_peer_id=FRIEND, order_id=order.id, status="applied"))
        await session.commit()
    sent = []

    async def to_client(peer_id, text_, random_id=None, **kwargs):
        sent.append(text_)

    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    noon = datetime.now(timezone.utc).replace(hour=9)
    # Вручено 15 дней назад, срок возврата 14 — награда положена.
    assert (await referral.grant_rewards(noon))["наград"] == 1
    assert "скидка 7 %" in sent[0]
    assert (await referral.grant_rewards(noon))["наград"] == 0
    async with clean() as session:
        assert len((await session.execute(select(ReferralReward))).scalars().all()) == 1
    draft = await friend_draft(OWNER)
    line = await referral.auto_apply(OWNER, draft)
    assert "скидка 7 %" in line and (await state.get_draft(OWNER)).items_total == 2790
