"""Часть Б, Б4: последний рубеж перед отправкой в Claude."""

from __future__ import annotations

import logging
from types import SimpleNamespace as NS

from app.modules.dialog import claude_client
from app.modules.ops import journal


async def test_guard_redacts_and_reports(clean, monkeypatch, caplog):
    sent = {}
    noted = []

    async def create(**kwargs):
        sent.update(kwargs)
        return NS(stop_reason="end_turn", content=[NS(type="text", text="ок")])

    monkeypatch.setattr(claude_client._client.messages, "create", create)
    monkeypatch.setattr(journal, "note_pii_redacted", lambda operation, count: noted.append((operation, count)))
    caplog.set_level(logging.WARNING)
    await claude_client.converse(
        [{"role": "user", "content": [{"type": "text", "text": "звоните 8 900 123-45-67, a@b.ru"},
                                      {"type": "tool_result", "tool_use_id": "t", "content": "[PHONE_1] ок"}]}],
        "Промпт без данных", [],
    )
    assert sent["messages"][0]["content"][0]["text"] == "звоните [REDACTED], [REDACTED]"
    assert sent["messages"][0]["content"][1]["content"] == "[PHONE_1] ок"
    assert noted == [("ход диалога", 2)]
    assert "8 900" not in caplog.text and "a@b.ru" not in caplog.text and "(2)" in caplog.text
