"""Расход Claude API: стоимость по ценам, запись каждого ответа, сброс в базу."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.modules.dialog import claude_client
from app.modules.ops import journal, llm_prices


def response(model="claude-sonnet-5-5", stop="end_turn", **usage):
    tokens = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0}
    tokens.update(usage)
    return SimpleNamespace(model=model, stop_reason=stop, stop_details=None, usage=SimpleNamespace(**tokens),
                           content=[])


@pytest.fixture(autouse=True)
def empty_buffer():
    journal._USAGE.clear()
    yield
    journal._USAGE.clear()


def test_cost_by_price_list():
    # 1000 входа × $2 + 500 выхода × $10 + 300 в кэш × $2,50 + 2000 из кэша × $0,20, за миллион
    assert llm_prices.cost_usd("claude-sonnet-5-5", input_tokens=1000, output_tokens=500,
                               cache_read_tokens=2000, cache_write_tokens=300) == Decimal("0.008150")
    assert llm_prices.cost_usd("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0,
                               cache_read_tokens=0, cache_write_tokens=0) == Decimal("2.000000")


def test_model_ids_are_matched_carefully():
    tokens = dict(input_tokens=0, output_tokens=1_000_000, cache_read_tokens=0, cache_write_tokens=0)
    assert llm_prices.cost_usd("claude-sonnet-5-5-20261001", **tokens) == Decimal("10.000000")
    # Неизвестная модель — без стоимости, а не по чужой цене.
    assert llm_prices.cost_usd("claude-opus-5-5", **tokens) is None
    assert llm_prices.cost_usd("", **tokens) is None


def test_every_response_is_noted_with_its_own_model():
    journal.note_llm_usage("ход диалога", "claude-sonnet-5-5",
                           response(input_tokens=1200, output_tokens=300, cache_read_input_tokens=8000))
    [row] = journal._USAGE
    assert (row["operation"], row["model"], row["input_tokens"], row["cache_read_tokens"]) == (
        "ход диалога", "claude-sonnet-5-5", 1200, 8000)
    assert row["cost_usd"] == Decimal("0.007000")  # 1200×2 + 300×10 + 8000×0,2 = 7000 / 1M


def test_noting_never_raises():
    journal.note_llm_usage("ход диалога", "x", object())  # ответ без usage
    journal.note_llm_usage("ход диалога", "x", None)
    assert all(r["cost_usd"] is None for r in journal._USAGE)


async def test_refusal_fallback_is_billed_twice(monkeypatch):
    calls = []

    async def create(**request):
        calls.append(request["model"])
        if len(calls) == 1:
            return response(model=request["model"], stop="refusal", input_tokens=900)
        return response(model=request["model"], input_tokens=900, output_tokens=120)

    monkeypatch.setattr(claude_client._client.messages, "create", create)
    monkeypatch.setattr(settings, "anthropic_fallback_model", "claude-sonnet-5")
    await claude_client._create("ход диалога", model="claude-sonnet-5-5", max_tokens=10, messages=[])
    assert calls == ["claude-sonnet-5-5", "claude-sonnet-5"]
    assert [(r["model"], r["stop_reason"]) for r in journal._USAGE] == [
        ("claude-sonnet-5-5", "refusal"), ("claude-sonnet-5", "end_turn")]


async def test_flush_writes_usage_and_the_view_shows_it(clean):
    journal.note_llm_usage("ассистент Ops", "claude-sonnet-5-5", response(input_tokens=500, output_tokens=50))
    await journal.flush()
    assert not journal._USAGE
    async with clean() as session:
        rows = (await session.execute(text(
            "select operation, model, input_tokens, output_tokens, cost_usd from analytics.v_llm_usage"
        ))).all()
    assert rows == [("ассистент Ops", "claude-sonnet-5-5", 500, 50, Decimal("0.001500"))]
