"""Модель вернула ответ без слов — один повторный запрос, а не «Не уверена…» сразу."""

from __future__ import annotations

from types import SimpleNamespace as NS

from app.modules.orders import conversation
from tests.test_auto_invoice import said, tool_use
from tests.test_vk_buttons import PEER, say, world  # noqa: F401 — фикстура


def silent():
    return NS(stop_reason="end_turn", content=[])


async def test_silence_is_asked_again(clean, world):
    world["script"] = [silent(), said("Подскажу с выбором чая 🙂")]
    await say("привет", 1)
    assert world["sent"][-1][0] == "Подскажу с выбором чая 🙂"
    # Последний запрос — со служебной просьбой ответить словами.
    assert "ответь клиенту словами" in str(world["model"][-1])


async def test_still_silent_falls_back(clean, world):
    world["script"] = [silent(), silent()]
    await say("привет", 1)
    assert world["sent"][-1][0] == conversation._NO_TEXT_FALLBACK


async def test_silence_after_a_tool_result_is_asked_again(clean, world):
    # Отменять нечего — инструмент отвечает модели, а та молчит.
    world["script"] = [tool_use("cancel_order"), silent(), said("Отменять нечего — заказов нет 🙂")]
    await say("отмените заказ", 1)
    assert world["sent"][-1][0] == "Отменять нечего — заказов нет 🙂"
