"""All roles use OpenAI; refusals never fall back to Anthropic."""
from types import SimpleNamespace as NS

import pytest

from app.modules.dialog import claude_client


@pytest.mark.parametrize("refused", [False, True])
async def test_roles_use_only_configured_openai_models(monkeypatch, refused):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return NS(stop_reason="refusal" if refused else "end_turn",
                  content=[NS(type="text", text="Ответ")], usage=None)

    monkeypatch.setattr(claude_client.openai_transport, "create", create)
    await claude_client.generate_reply("Чай")
    await claude_client.generate_dialog_report("Выбирал чай")
    await claude_client.converse([{"role": "user", "content": "Чай"}], "Продавец", [])
    await claude_client.ops_assistant_turn([{"role": "user", "content": "Сбои?"}], "Ops", [])
    assert [c["model"] for c in calls] == ["gpt-6-luna"] * 3 + ["gpt-6.1-sol"]
    assert all(c["output_config"]["effort"] == "low" for c in calls)
