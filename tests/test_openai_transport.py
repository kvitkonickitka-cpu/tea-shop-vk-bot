import json
from types import SimpleNamespace as NS

import httpx
import pytest
from openai import AsyncOpenAI

from app.modules.dialog import openai_transport as transport
from app.modules.ops import journal, llm_prices


def reply(output, *, status="completed", cached=0):
    return dict(id="resp_test", object="response", created_at=1, model="gpt-6-luna",
        status=status, output=output, parallel_tool_calls=False, tool_choice="auto",
        tools=[], usage=dict(input_tokens=1000, output_tokens=100, total_tokens=1100,
            input_tokens_details=dict(cached_tokens=cached), output_tokens_details=dict(reasoning_tokens=20)))


def message(text):
    return dict(id="msg_test", type="message", role="assistant", status="completed",
                content=[dict(type="output_text", text=text, annotations=[])])


async def test_http_tool_roundtrip_keeps_reasoning_and_call_ids(monkeypatch):
    requests = []
    outputs = [
        [dict(id="rs_test", type="reasoning", summary=[], encrypted_content="opaque"),
         dict(id="fc_test", type="function_call", call_id="call_1", name="catalog",
              arguments='{"query":"чай"}', status="completed")],
        [message("Есть улун")],
    ]

    def handle(request):
        requests.append(json.loads(request.content))
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json=reply(outputs.pop(0), cached=200))

    sdk = AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(transport, "_client", sdk)
    tools = [dict(name="catalog", description="Каталог", input_schema=dict(type="object", properties={}))]
    history = [{"role": "user", "content": [{"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}, {"type": "text", "text": "Чай"}]}]
    request = dict(model="gpt-6-luna", system=[dict(type="text", text="A"), dict(type="text", text="B")],
                   messages=history, tools=tools, max_tokens=1000, output_config={"effort": "low"})
    first = await transport.create(**request)
    assert first.stop_reason == "tool_use"
    call = first.content[1]
    assert call.id == "call_1" and call.input == {"query": "чай"}
    assert first.usage.input_tokens == 800 and first.usage.cache_read_input_tokens == 200
    history.extend([{"role": "assistant", "content": first.content},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "content": "Улун"}]}])
    second = await transport.create(**request, tool_choice={"type": "none"})
    assert second.content[0].text == "Есть улун"
    assert requests[0]["instructions"] == "AB"
    assert requests[0]["input"][0]["content"][0]["type"] == "input_image"
    assert requests[0]["parallel_tool_calls"] is False and requests[0]["store"] is False
    assert requests[1]["tool_choice"] == "none"
    assert requests[1]["input"][1]["encrypted_content"] == "opaque"
    assert requests[1]["input"][-1] == dict(type="function_call_output", call_id="call_1", output="Улун")
    await sdk.close()


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled"])
def test_incomplete_response_cannot_execute_orders(status):
    with pytest.raises(RuntimeError, match="not completed"):
        transport.adapt(NS(status=status))


def test_openai_timeouts_are_classified():
    from openai import APITimeoutError
    assert journal.classify(APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))) == ("timeout", None)


def test_cached_tokens_not_billed_twice():
    from decimal import Decimal
    assert llm_prices.cost_usd("gpt-6-luna", input_tokens=800, output_tokens=100,
        cache_read_tokens=200, cache_write_tokens=0) == Decimal("0.000132")


async def test_proxy_uses_separate_secret(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(transport, "_client", None)
    monkeypatch.setattr(settings, "openai_api_key", "test-api-key")
    monkeypatch.setattr(settings, "openai_base_url", "https://proxy.example/openai/v1")
    monkeypatch.setattr(settings, "openai_proxy_secret", "test-proxy-secret")
    sdk = transport.client()
    assert str(sdk.base_url) == "https://proxy.example/openai/v1/"
    assert sdk.default_headers["X-Tea-Proxy-Secret"] == "test-proxy-secret"
    assert sdk.auth_headers["Authorization"] == "Bearer test-api-key"
    await sdk.close()
