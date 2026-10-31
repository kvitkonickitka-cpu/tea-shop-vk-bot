"""Кэш промпта Anthropic: модель видит тот же текст, в общий кэш — только общее.

Проверяем с пристрастием, на настоящих ходах бота (модель подменена на
уровне HTTP-клиента, весь путь — боевой):

- склеенный текст блоков `system` посимвольно равен тому промпту, что
  собрал ход, — кэш не меняет, что видит модель;
- метка кэша стоит ровно на неизменной части: базовый промпт, каталог; в
  ней нет ни черновика, ни меток клиента, ни его покупок — эту часть
  читают все клиенты;
- неизменная часть одинакова у всех кругов хода и у разных клиентов;
- выключенный кэш — запрос как раньше: строкой, без меток;
- последний рубеж по-прежнему чистит промпт, разбитый на блоки;
- у ассистента Ops кэшируется весь промпт и переписка вопроса.
"""

from __future__ import annotations

import json

import pytest

from app import privacy
from app.core.config import settings
from app.modules.dialog import claude_client
from app.modules.orders import conversation
from tests.test_auto_invoice import said, tool_use
from tests.test_privacy_interceptor import REAL_CONVERSE, wire  # noqa: F401
from tests.test_storefront_points import ADDRESS, shop, storefront  # noqa: F401
from tests.test_vk_buttons import FULL, PEER, listing, say, world  # noqa: F401

CLIENT_MARKERS = ("[NAME_", "[PHONE_", "[EMAIL_", "[ADDR_", "Черновик заказа", "Активного черновика",
                  "Покупки клиента", "Иванов", "ivanov@")


@pytest.fixture
def prompts(wire, monkeypatch):
    """Что собрал ход — до кэша: промпт строкой и граница неизменной части."""
    built = []

    async def spy(messages, system_prompt, tools, **kwargs):
        built.append((system_prompt, kwargs.get("cache_upto")))
        return await REAL_CONVERSE(messages, system_prompt, tools, **kwargs)

    monkeypatch.setattr(conversation.claude_client, "converse", spy)
    return built


def _text(system) -> str:
    return system if isinstance(system, str) else "".join(block["text"] for block in system)


async def _order_turns(wire):
    await listing(recipient=False)
    wire["script"] = [
        tool_use("set_delivery_method", method="ozon_pvz", address="Краснодар", pickup_point="1"),
        tool_use("set_recipient", name="[NAME_1]", phone="[PHONE_1]", email="[EMAIL_1]"),
    ]
    await say("1, Иванов Иван, 89001234567, ivanov@mail.ru", 1)


async def test_model_sees_the_same_text_and_only_shared_part_is_cached(clean, wire, prompts, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_prompt_cache", True)
    await _order_turns(wire)
    assert len(wire["requests"]) >= 2 and len(prompts) == len(wire["requests"])

    statics = set()
    for request, (built, cache_upto) in zip(wire["requests"], prompts):
        system = request["system"]
        assert isinstance(system, list) and len(system) == 2
        # Модель видит ровно собранный промпт — кэш только делит его на блоки.
        assert _text(system) == built
        shared, personal = system
        assert shared["cache_control"] == {"type": "ephemeral"} and "cache_control" not in personal
        assert shared["text"] == built[:cache_upto]
        # Общая часть — без данных и состояния этого клиента. Метки вроде
        # [NAME_1] в ней есть только в инструкции модели о метках — это
        # пример, не клиент; вне инструкции их быть не должно.
        outside = shared["text"].replace(conversation._PII_PROMPT, "")
        assert conversation._PII_PROMPT in shared["text"]
        for marker in CLIENT_MARKERS:
            assert marker not in outside, marker
        assert shared["text"].startswith(claude_client._BASE_SYSTEM_PROMPT)
        assert "Текущий ассортимент" in shared["text"]
        # Состояние клиента — во второй части.
        assert "черновик" in personal["text"].lower()
        statics.add(shared["text"])
        # Метки верхнего уровня у бота нет: хвост переписки меняется каждый ход.
        assert "cache_control" not in request
    assert len(statics) == 1  # одна и та же общая часть на всех кругах и ходах


async def test_shared_part_is_the_same_for_another_client(clean, wire, prompts, monkeypatch):
    from app.modules.dialog import inbound

    monkeypatch.setattr(settings, "anthropic_prompt_cache", True)
    await _order_turns(wire)
    first = wire["requests"][0]["system"][0]["text"]
    before = len(wire["requests"])
    # Другой клиент, без черновика, — консультация.
    wire["script"] = [said("Здравствуйте! Подскажу 🙂")]
    await inbound.accept("other-1", {"peer_id": PEER + 1, "text": "привет, посоветуйте чай",
                                     "conversation_message_id": 1}, FULL)
    other = wire["requests"][before:]
    assert other, "второй клиент не дошёл до модели"
    shared, personal = other[0]["system"]
    assert shared["text"] == first
    assert "Активного черновика заказа у клиента нет" in personal["text"]


async def test_cache_off_is_the_old_request(clean, wire, prompts, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_prompt_cache", False)
    await _order_turns(wire)
    for request, (built, _) in zip(wire["requests"], prompts):
        assert request["system"] == built
        assert "cache_control" not in json.dumps(request, ensure_ascii=False, default=str)


async def test_guard_still_scrubs_a_split_prompt(monkeypatch):
    seen, redacted = [], []

    async def create(**kwargs):
        seen.append(kwargs)
        return said("ok")

    monkeypatch.setattr(settings, "anthropic_prompt_cache", True)
    monkeypatch.setattr(claude_client.openai_transport, "create", create)
    monkeypatch.setattr(claude_client.journal, "note_pii_redacted", lambda op, n: redacted.append(n))
    shared = "Ты продавец чая. " * 60
    personal = "\n\nКлиент оставил телефон 89001234567 и почту a.b@mail.ru."
    await claude_client.converse([{"role": "user", "content": "привет"}], shared + personal, [],
                                 cache_upto=len(shared))
    system = seen[0]["system"]
    assert system[0]["text"] == shared and system[0]["cache_control"] == {"type": "ephemeral"}
    assert "89001234567" not in system[1]["text"] and "a.b@mail.ru" not in system[1]["text"]
    assert privacy.REDACTED in system[1]["text"] and redacted == [2]


@pytest.mark.parametrize("upto", [0, None, 10**6])
async def test_edges_of_the_split(monkeypatch, upto):
    seen = []

    async def create(**kwargs):
        seen.append(kwargs)
        return said("ok")

    monkeypatch.setattr(settings, "anthropic_prompt_cache", True)
    monkeypatch.setattr(claude_client.openai_transport, "create", create)
    await claude_client.converse([{"role": "user", "content": "привет"}], "Промпт целиком.", [], cache_upto=upto)
    system = seen[0]["system"]
    if upto == 0:
        assert system == "Промпт целиком."  # граница не задана — без кэша
    else:
        assert system == [{"type": "text", "text": "Промпт целиком.", "cache_control": {"type": "ephemeral"}}]


async def test_ops_assistant_caches_prompt_and_conversation(monkeypatch):
    from app.modules.ops.assistant import service, tools

    seen = []

    async def create(**kwargs):
        seen.append(kwargs)
        return said("Сбоев нет.")

    monkeypatch.setattr(settings, "anthropic_prompt_cache", True)
    monkeypatch.setattr(claude_client.openai_transport, "create", create)
    await claude_client.ops_assistant_turn([{"role": "user", "content": "что со сбоями?"}], service.PROMPT, tools.TOOLS)
    request = seen[0]
    assert request["system"] == [{"type": "text", "text": service.PROMPT, "cache_control": {"type": "ephemeral"}}]
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["tools"] == tools.TOOLS  # инструменты те же — иначе кэш бесполезен
