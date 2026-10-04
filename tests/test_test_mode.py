"""Команды тестовых аккаунтов: /новый, /сброс, /постоянный — и пометка 🧪."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.modules.analytics import service as analytics
from app.modules.dialog import history as dialog_history, test_mode
from app.modules.orders import purchases, repeat_delivery, state
from app.modules.orders.state import OrderDraft
from tests.test_repeat_delivery import make_order
from tests.test_vk_buttons import FULL, say, world  # noqa: F401 — фикстура

PEER = 9950  # тот же клиент, что у make_order


@pytest.fixture
def tester(monkeypatch):
    monkeypatch.setattr(settings, "test_vk_ids", str(PEER))
    monkeypatch.setattr(analytics, "_test_ids", None)
    monkeypatch.setattr(settings, "purchase_history_in_prompt_enabled", True)


async def send(text: str, n: int):
    from app.modules.dialog import inbound

    await inbound.accept(f"tm{n}", {"peer_id": PEER, "text": text, "conversation_message_id": n}, FULL)


async def test_fresh_forgets_past_orders_and_regular_brings_them_back(clean, world, tester):
    await make_order(clean, minutes_ago=600)
    assert await repeat_delivery.last_for(PEER) is not None

    await send("/новый", 1)
    assert world["sent"][-1][0].startswith("🧪 Тестовый режим: вы новый клиент")
    assert world["model"] == []  # команду модель не видит
    assert await repeat_delivery.last_for(PEER) is None
    assert await repeat_delivery.last_recipient_for(PEER) is None
    assert await purchases.context(PEER) == ""

    # Новый заказ после отметки — уже учитывается.
    await make_order(clean, minutes_ago=0, payment_id="pay-new")
    assert await repeat_delivery.last_for(PEER) is not None

    await send("/постоянный", 2)
    assert world["sent"][-1][0].startswith("🧪 Тестовый режим: снова постоянный клиент")
    assert await test_mode.fresh_since(PEER) is None


async def test_reset_clears_draft_unpaid_orders_and_history(clean, world, tester):
    await dialog_history.append_exchange(PEER, "хочу чай", "Записала.")
    await state.set_draft(PEER, OrderDraft(items=[{"name": "Да Хун Пао", "quantity": 1, "price": 1500}],
                                           items_total=1500, stage="awaiting_delivery", details={}))
    unpaid = await make_order(clean, minutes_ago=5, status="awaiting_payment", payment_status="pending",
                              payment_id="pay-unpaid")
    await send("/сброс", 1)
    assert await state.get_draft(PEER) is None
    assert await dialog_history.get_history(PEER) == []
    assert f"№{unpaid.id}" in world["sent"][-1][0]
    # Сброс режим «новый» не включает.
    assert await test_mode.fresh_since(PEER) is None


async def test_commands_do_nothing_for_real_clients(clean, world, monkeypatch):
    monkeypatch.setattr(settings, "test_vk_ids", "")
    monkeypatch.setattr(analytics, "_test_ids", None)
    await make_order(clean, minutes_ago=600)
    await send("/новый", 1)
    # Обычное сообщение: его видит модель, режим не меняется.
    assert world["model"] and await repeat_delivery.last_for(PEER) is not None


async def test_unresolved_test_id_is_retried(monkeypatch):
    from app.modules.dialog import vk_client

    calls = []

    async def resolve(raw):
        calls.append(raw)
        return None if len(calls) == 1 else 777

    monkeypatch.setattr(vk_client, "resolve_user_id", resolve)
    monkeypatch.setattr(settings, "test_vk_ids", "kvitko")
    monkeypatch.setattr(analytics, "_test_ids", None)
    assert await analytics.test_peer_ids() == set()
    assert (await analytics.test_ids_status())["не переведено"] == 1
    # Повтор — после паузы, а не до перезапуска контейнера.
    monkeypatch.setattr(analytics, "_retry_at", 0.0)
    assert await analytics.test_peer_ids() == {777}
