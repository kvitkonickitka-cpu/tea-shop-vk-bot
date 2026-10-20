"""Пакет C, третья часть: отзывы после покупки (C7) и автоподтверждение Ozon (C8)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core import flags
from app.core.config import settings
from app.messages.models import FunnelEvent
from app.modules.analytics import service as analytics
from app.modules.delivery import ozon_approve, ozon_client
from app.modules.dialog import escalation_state, telegram_client, vk_client
from app.modules.orders import buttons, feedback, retention, reviews
from app.modules.orders.models import Order, OrderFeedback, OrderRating

PEER, OTHER = 9971, 9972


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(settings, "test_vk_ids", str(PEER))
    monkeypatch.setattr(analytics, "_test_ids", None)
    monkeypatch.setattr(settings, "telegram_manager_chat_id", "-200")
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100")
    box = {"tg": [], "notify": [], "wall": []}

    async def tg_call(method, payload=None, **kwargs):
        box["tg"].append((method, payload))
        return {"message_id": 1}

    async def notify(kind, text_, **kwargs):
        box["notify"].append(text_)
        return True

    async def wall_post(message):
        box["wall"].append(message)
        return 77

    async def first_name(user_id):
        return "Иван"

    monkeypatch.setattr(telegram_client, "call", tg_call)
    monkeypatch.setattr("app.messages.manager.notify", notify)
    monkeypatch.setattr(vk_client, "wall_post", wall_post)
    monkeypatch.setattr(vk_client, "first_name", first_name)
    return box


async def delivered(db, peer=PEER, **fields) -> Order:
    now = datetime.now(timezone.utc)
    async with db() as session:
        order = Order(peer_id=peer, items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 1500}],
                      items_total=1500, delivery_cost=121, total=1621, delivery_method="ozon_pvz",
                      status="confirmed", payment_status="succeeded", details={}, paid_at=now - timedelta(days=8),
                      handed_over_at=now - timedelta(days=7), delivered_at=now - timedelta(days=3), **fields)
        session.add(order)
        await session.commit()
        return order


async def events(db) -> list[str]:
    async with db() as session:
        return list((await session.execute(select(FunnelEvent.event).order_by(FunnelEvent.id))).scalars())


async def test_stars_replace_three_words_when_flag_is_on(clean, world):
    order = await delivered(clean)
    message = await feedback._ask_builder(order)()
    assert "Очень понравился" in json.dumps(message.keyboard, ensure_ascii=False)
    await flags.set_mode(flags.REVIEW_FLOW, flags.ALLOWLIST, by="test")
    message = await feedback._ask_builder(order)()
    labels = [b["action"]["label"] for b in message.keyboard["buttons"][0]]
    assert labels == ["1 ⭐", "2 ⭐", "3 ⭐", "4 ⭐", "5 ⭐"]


async def test_five_stars_ask_for_review_then_consent_buttons_then_card(clean, world):
    await flags.set_mode(flags.REVIEW_FLOW, flags.ALLOWLIST, by="test")
    order = await delivered(clean)
    press = await buttons._on_rate(PEER, {"a": "rate", "o": order.id, "r": "5"})
    assert press.reply.startswith("Спасибо, очень приятно!")
    async with clean() as session:
        rating = await session.get(OrderRating, order.id)
    assert (rating.rating, rating.score) == ("great", 5)
    result = await feedback.save(PEER, {"order_id": order.id, "text": "Очень цветочный, вкусный", "publish_consent": "unknown"})
    assert "кнопками" in result
    stashed = buttons._stashed.pop(PEER)
    assert [b["action"]["label"] for b in stashed["buttons"][0]] == ["С именем из ВК", "Анонимно", "Не публиковать"]
    world["notify"].clear()
    press = await buttons._on_review_consent(PEER, {"a": "rv_consent", "o": order.id, "c": "named"})
    assert press.reply.startswith("Спасибо! Опубликуем с вашим именем")
    method, payload = world["tg"][-1]
    assert method == "sendMessage" and payload["chat_id"] == "-200"
    assert payload["reply_markup"]["inline_keyboard"][0][0] == {"text": "Опубликовал вручную",
                                                               "callback_data": f"rv:done:{order.id}"}
    assert "Очень цветочный" in payload["text"] and "Оценка: 5 из 5" in payload["text"]
    assert world["wall"] == []  # сам бот ничего не публикует
    assert await events(clean) == ["rated", "review_requested", "review_saved", "review_consent"]


async def test_manager_button_publishes(clean, world, monkeypatch):
    order = await delivered(clean)
    async with clean() as session:
        session.add(OrderFeedback(order_id=order.id, peer_id=PEER, text="Отличный улун", publish_consent="named"))
        session.add(OrderRating(order_id=order.id, peer_id=PEER, rating="great", source="button", score=5))
        await session.commit()
    callback = {"id": "cb1", "data": f"rv:done:{order.id}", "message": {"chat": {"id": -200}, "message_id": 9}}
    # Из чужого чата кнопку не принимаем.
    assert not await reviews.handle_callback({**callback, "message": {"chat": {"id": -999}, "message_id": 9}})
    assert await reviews.handle_callback(callback)
    assert world["wall"] == []
    async with clean() as session:
        row = await session.get(OrderFeedback, order.id)
    assert row.published_how == "manual" and row.published_at is not None
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "Отмечено: опубликовано вручную ✅"}) in world["tg"]
    # Режим wall: бот публикует сам — с именем из ВК, без правки текста.
    other = await delivered(clean)
    async with clean() as session:
        session.add(OrderFeedback(order_id=other.id, peer_id=PEER, text="Беру ещё", publish_consent="anonymous"))
        await session.commit()
    monkeypatch.setattr(settings, "review_publish_mode", "wall")
    await reviews.handle_callback({"id": "cb2", "data": f"rv:pub:{other.id}",
                                   "message": {"chat": {"id": -200}, "message_id": 10}})
    assert world["wall"] == ["Отзыв покупателя\n«Беру ещё»\n— покупатель"]
    assert (await events(clean)).count("review_published") == 2


async def test_low_rating_goes_to_manager_and_pauses_touches(clean, world):
    await flags.set_mode(flags.REVIEW_FLOW, flags.ALLOWLIST, by="test")
    order = await delivered(clean)
    press = await buttons._on_rate(PEER, {"a": "rate", "o": order.id, "r": "2"})
    assert press.reply.startswith("Нам очень жаль")
    assert await escalation_state.is_open(PEER)
    assert any("Клиент поставил 2 из 5" in text for text in world["notify"])
    assert "complaint_from_rating" in await events(clean)
    now = datetime.now(timezone.utc)
    await escalation_state.mark_resolved(PEER)
    assert "жалоба" in (await retention.blocker(PEER, now + timedelta(days=5)) or "")
    assert await reviews.close_complaints(PEER) == 1
    assert "жалоба" not in (await retention.blocker(PEER, now + timedelta(days=5)) or "")


@pytest.fixture
def ozon(monkeypatch):
    calls = []

    async def call(path, payload):
        calls.append((path, payload))
        return {}

    monkeypatch.setattr(ozon_client, "call", call)
    return calls


async def test_ozon_approve_dry_run_then_live(clean, world, ozon, monkeypatch):
    order = await delivered(clean, ozon_posting="0123-0001-1")
    assert (await ozon_approve.after_packed(order.id))["skipped"] == "флаг выключен"
    await flags.set_mode(flags.OZON_AUTO_APPROVE, flags.ALLOWLIST, by="test")
    result = await ozon_approve.after_packed(order.id)
    assert result == {"dry_run": True, "request": {"path": "/v1/posting/approve",
                                                   "body": {"posting_number": "0123-0001-1"}}}
    assert ozon == []
    stranger = await delivered(clean, peer=OTHER, ozon_posting="0123-0002-1")
    assert (await ozon_approve.after_packed(stranger.id))["skipped"] == "allowlist: не тестовый заказ"
    # on без OZON_APPROVE_LIVE — всё равно dry-run.
    await flags.set_mode(flags.OZON_AUTO_APPROVE, flags.ON, by="test")
    assert (await ozon_approve.after_packed(stranger.id))["dry_run"] and ozon == []
    monkeypatch.setattr(settings, "ozon_approve_live", True)
    assert (await ozon_approve.after_packed(stranger.id)) == {"approved": True}
    assert ozon == [("/v1/posting/approve", {"posting_number": "0123-0002-1"})]
    assert (await ozon_approve.after_packed(stranger.id))["skipped"] == "уже подтверждено"
    assert "ozon_approve_dry_run" in await events(clean) and "ozon_approved" in await events(clean)


async def test_ozon_approve_error_goes_to_ops(clean, world, monkeypatch):
    order = await delivered(clean, ozon_posting="0123-0003-1")
    await flags.set_mode(flags.OZON_AUTO_APPROVE, flags.ON, by="test")
    monkeypatch.setattr(settings, "ozon_approve_live", True)

    async def broken(path, payload):
        raise ozon_client.OzonError("HTTP 400") if hasattr(ozon_client, "OzonError") else RuntimeError("HTTP 400")

    monkeypatch.setattr(ozon_client, "call", broken)
    assert "error" in await ozon_approve.after_packed(order.id)
    assert any("Ozon не подтвердил отправление" in text for text in world["notify"])
