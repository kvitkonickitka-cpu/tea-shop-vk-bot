"""Аналитика, А1: клиенты, псевдонимный ключ, тестовые данные."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core import client_key as client_key_module
from app.core.config import settings
from app.modules.analytics import service as analytics
from app.modules.analytics.models import Client
from app.modules.orders import repeat_delivery
from app.modules.orders.models import Order
from tests.test_feedback_repeat_optout import NOW, PEER, make_order

TEST_PEER = 362545925


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setattr(settings, "client_key_secret", "s3cret")
    monkeypatch.setattr(settings, "test_vk_ids", f"{TEST_PEER}, 100")
    monkeypatch.setattr(analytics, "_test_ids", None)


def test_client_key_is_stable_and_needs_a_secret(monkeypatch):
    key = client_key_module.client_key(PEER)
    assert key == client_key_module.client_key(str(PEER)) and len(key) == 32
    assert key != client_key_module.client_key(PEER + 1)
    monkeypatch.setattr(settings, "client_key_secret", "")
    assert client_key_module.client_key(PEER) is None


async def test_sync_marks_test_accounts_after_preview(clean):
    real = await make_order(clean, ozon_posting="r-1")
    test = await make_order(clean, peer_id=TEST_PEER, ozon_posting="t-1")
    preview = await analytics.sync(apply=False)
    assert preview["номера"] == [test.id]
    async with clean() as session:
        assert not (await session.get(Order, test.id)).is_test  # предпросмотр ничего не меняет
    await analytics.sync()
    async with clean() as session:
        assert (await session.get(Order, test.id)).is_test and not (await session.get(Order, real.id)).is_test
        clients = {c.peer_id: c for c in (await session.execute(select(Client))).scalars().all()}
    assert clients[TEST_PEER].is_test and not clients[PEER].is_test
    assert clients[PEER].client_key == client_key_module.client_key(PEER)
    assert (await analytics.sync())["помечено заказов"] == 0  # повтор ничего не меняет


async def test_paid_before_rule(clean, monkeypatch):
    monkeypatch.setattr(settings, "test_paid_before", (NOW - timedelta(days=1)).isoformat())
    old = await make_order(clean, ozon_posting="o-1", paid_at=NOW - timedelta(days=2))
    new = await make_order(clean, ozon_posting="o-2", paid_at=NOW)
    assert (await analytics.sync(apply=False))["номера"] == [old.id]
    assert new.id


async def test_test_orders_not_offered_to_real_clients(clean):
    # Тестовый заказ у обычного клиента (например, аккаунт убрали из списка) —
    # «как в прошлый раз» его не предлагает; тестовому аккаунту — предлагает.
    await make_order(clean, ozon_posting="x-1", is_test=True,
                     details={"address": "Краснодар", "ozon_point_id": 1, "ozon_point_address": "Красная, 1"})
    assert await repeat_delivery._successful_orders(PEER) == []
    await make_order(clean, peer_id=TEST_PEER, ozon_posting="x-2", is_test=True)
    assert len(await repeat_delivery._successful_orders(TEST_PEER)) == 1


async def test_ensure_client_once(clean):
    await analytics.ensure_client(PEER, ref="spring", ref_source="ads")
    await analytics.ensure_client(PEER, ref="other", ref_source="post")
    async with clean() as session:
        row = await session.get(Client, PEER)
    assert (row.ref, row.ref_source) == ("spring", "ads")


async def test_ref_is_kept_from_the_first_contact_only(clean, monkeypatch):
    from app.modules.dialog import inbound

    async def no_turn(peer_id, started):
        return True

    monkeypatch.setattr(inbound, "_run_turn", no_turn)
    monkeypatch.setattr(inbound, "is_enabled", lambda: False)
    await inbound.accept("e1", {"peer_id": PEER, "text": "привет", "ref": "autumn", "ref_source": "post"})
    await inbound.accept("e2", {"peer_id": PEER, "text": "ещё", "ref": "other", "ref_source": "ads"})
    await inbound.accept("e3", {"peer_id": PEER + 1, "text": "сам пришёл"})
    async with clean() as session:
        clients = {c.peer_id: c for c in (await session.execute(select(Client))).scalars().all()}
    assert (clients[PEER].ref, clients[PEER].ref_source) == ("autumn", "post")
    assert clients[PEER].client_key == client_key_module.client_key(PEER)
    assert (clients[PEER + 1].ref, clients[PEER + 1].ref_source) == (None, None)  # органика
