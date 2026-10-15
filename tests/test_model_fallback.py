"""Отказ модели (stop_reason "refusal") — повтор на запасной, а не «заминка» клиенту."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.core.config import settings
from app.modules.dialog import claude_client


def refusal():
    return NS(stop_reason="refusal", stop_details=NS(category="general_harms", explanation=""), content=[],
              usage=NS(input_tokens=1, output_tokens=0))


def said(text):
    return NS(stop_reason="end_turn", stop_details=None, content=[NS(type="text", text=text)],
              usage=NS(input_tokens=1, output_tokens=1))


@pytest.fixture
def calls(monkeypatch):
    box = {"models": [], "script": []}

    async def create(**kwargs):
        box["models"].append(kwargs["model"])
        return box["script"].pop(0)

    monkeypatch.setattr(claude_client._client.messages, "create", create)
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "anthropic_fallback_model", "claude-sonnet-5")
    return box


async def test_dialog_turn_retries_on_the_fallback_model(calls):
    calls["script"] = [refusal(), said("Подскажу 🙂")]
    response = await claude_client.converse([{"role": "user", "content": "Чай от давления?"}], "Ты продавец чая.", [])
    assert calls["models"] == ["claude-sonnet-5-5", "claude-sonnet-5"]
    assert claude_client.extract_text(response) == "Подскажу 🙂"


async def test_no_retry_without_refusal(calls):
    calls["script"] = [said("Привет!")]
    await claude_client.converse([{"role": "user", "content": "Привет"}], "Ты продавец чая.", [])
    assert calls["models"] == ["claude-sonnet-5-5"]


async def test_no_retry_when_fallback_is_the_same_model(calls, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_fallback_model", "claude-sonnet-5-5")
    calls["script"] = [refusal()]
    response = await claude_client.converse([{"role": "user", "content": "?"}], "Ты продавец чая.", [])
    assert calls["models"] == ["claude-sonnet-5-5"]
    assert response.stop_reason == "refusal"


async def test_dialog_report_and_ops_assistant_retry_too(calls):
    calls["script"] = [refusal(), said("Тема: чай\nИтог: купил")]
    assert "купил" in await claude_client.generate_dialog_report("клиент: хочу чай")
    calls["script"] = [refusal(), said("Сбоев нет.")]
    response = await claude_client.ops_assistant_turn([{"role": "user", "content": "что со сбоями?"}], "Дежурный.", [])
    assert claude_client.extract_text(response) == "Сбоев нет."
    assert calls["models"] == ["claude-sonnet-5-5", "claude-sonnet-5"] * 2
