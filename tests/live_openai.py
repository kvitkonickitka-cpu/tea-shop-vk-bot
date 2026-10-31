"""Explicit paid evaluation: pytest tests/live_openai.py (never normal discovery).

Only synthetic test data; all non-OpenAI HTTP requests are blocked.
"""
import os

import httpx
import pytest

from app.core.config import settings
from app.modules.dialog import claude_client, openai_transport
from app.modules.orders import conversation, state
from tests.test_vk_buttons import world, PEER, listing  # noqa: F401

REAL_CONVERSE = claude_client.converse
pytestmark = pytest.mark.skipif(os.environ.get("RUN_OPENAI_LIVE") != "1", reason="Paid test is opt-in")


@pytest.fixture
async def live(monkeypatch, world):
    original_send = httpx.AsyncClient.send

    async def send(client, request, **kwargs):
        if request.url.host != "api.openai.com":
            raise RuntimeError("External side effect blocked in paid evaluation")
        return await original_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(claude_client, "converse", REAL_CONVERSE)
    monkeypatch.setattr(openai_transport, "_client", None)
    monkeypatch.setattr(settings, "openai_max_tokens", 2000)
    monkeypatch.setattr(settings, "ops_assistant_max_tokens", 2000)
    calls = []
    original_create = openai_transport.create

    async def counted(**kwargs):
        assert len(calls) < 15, "Paid request budget exceeded"
        response = await original_create(**kwargs)
        calls.append(response)
        return response

    monkeypatch.setattr(openai_transport, "create", counted)
    yield calls
    if openai_transport._client:
        await openai_transport._client.close()


async def test_sales_consultation(clean, live):
    answer = await conversation.handle_turn(PEER, "Хочу улун. Что есть и сколько стоит?")
    assert live and answer.strip()
    assert "1500" in answer.replace(" ", "").replace("\u00a0", "")
    assert conversation._NO_TEXT_FALLBACK not in answer


async def test_pickup_selection_preserves_order(clean, live):
    await listing(recipient=False)
    answer = await conversation.handle_turn(PEER, "Выбираю первый пункт выдачи из списка")
    draft = await state.get_draft(PEER)
    assert live and answer.strip()
    assert draft and (draft.details.get("ozon_point_id") or draft.details.get("delivery_point"))
    assert conversation._NO_TEXT_FALLBACK not in answer


async def test_report_and_ops_real_tools(clean, live, monkeypatch):
    from app.modules.ops.assistant import service
    from tests.test_ops_assistant import message, ME

    report = await claude_client.generate_dialog_report("Клиент спросил цену улуна. Продавец ответил: 1500 рублей. Заказ не оформлен.")
    assert report.strip()
    result = await service.answer(message("Проверь ошибки внешних сервисов за последний час через service_errors."), ME)
    assert result["text"].strip() and result["tool_calls"] >= 1
    assert any(r.model.startswith("gpt-6.1-sol") for r in live)
