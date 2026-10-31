"""Responses API transport for the application's existing conversation blocks.

Keep order execution in the application. The model only proposes function calls.
The compatibility blocks allow existing order and Ops guards to stay in place.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from openai import AsyncOpenAI

from app.core.config import settings

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            default_headers=({"X-Tea-Proxy-Secret": settings.openai_proxy_secret}
                             if settings.openai_proxy_secret else None),
            timeout=settings.openai_timeout_seconds,
            max_retries=1,
        )
    return _client


def input_items(messages: list[dict]) -> list[dict]:
    items = []
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            if content.strip():
                items.append({"role": role, "content": content})
            continue
        pending = []

        def flush():
            if pending:
                items.append({"role": role, "content": pending.copy()})
                pending.clear()

        for block in content:
            raw = getattr(block, "response_item", None)
            if raw is not None:
                flush()
                items.append(raw)
                continue
            b = block if isinstance(block, dict) else vars(block)
            kind = b.get("type")
            if kind == "text":
                pending.append({"type": "input_text", "text": b["text"]})
            elif kind == "image":
                source = b["source"]
                if source.get("type") != "base64":
                    raise ValueError("Only downloaded base64 images are supported")
                pending.append({"type": "input_image", "image_url":
                    f"data:{source['media_type']};base64,{source['data']}"})
            elif kind == "tool_result":
                flush()
                output = b.get("content", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                items.append({"type": "function_call_output", "call_id": b["tool_use_id"], "output": output})
            elif kind == "tool_use":
                flush()
                items.append({"type": "function_call", "call_id": b["id"],
                              "name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)})
            else:
                raise ValueError(f"Unsupported conversation block: {kind}")
        flush()
    return items


def adapt(response):
    # Never execute a function from an incomplete/failed response.
    if response.status not in ("completed",):
        raise RuntimeError("OpenAI response was not completed")
    blocks = []
    refused = False
    for item in response.output:
        raw = item.model_dump(exclude_none=True)
        if item.type == "function_call":
            arguments = json.loads(item.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Function arguments must be an object")
            blocks.append(SimpleNamespace(type="tool_use", id=item.call_id,
                name=item.name, input=arguments, response_item=raw))
        elif item.type == "message":
            texts = []
            for part in item.content:
                if part.type == "output_text":
                    texts.append(part.text)
                elif part.type == "refusal":
                    refused = True
                    texts.append(part.refusal)
            blocks.append(SimpleNamespace(type="text", text="\n".join(texts), response_item=raw))
        elif item.type == "reasoning":
            blocks.append(SimpleNamespace(type="reasoning", response_item=raw))
        else:
            raise ValueError(f"Unexpected OpenAI output type: {item.type}")
    if refused:
        # A refused response must not execute any accompanying function calls.
        blocks = [b for b in blocks if b.type != "tool_use"]
    usage = response.usage
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    return SimpleNamespace(
        content=blocks, model=response.model,
        stop_reason="refusal" if refused else ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"),
        usage=SimpleNamespace(
            input_tokens=max(0, (getattr(usage, "input_tokens", 0) or 0) - cached),
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=cached, cache_creation_input_tokens=0,
        ),
    )


async def create(**request):
    system = request.get("system", "")
    if isinstance(system, list):
        system = "".join(b.get("text", "") for b in system)
    payload = dict(
        model=request["model"], instructions=system,
        input=input_items(request["messages"]),
        max_output_tokens=request["max_tokens"],
        store=False, include=["reasoning.encrypted_content"],
    )
    effort = request.get("output_config", {}).get("effort")
    if effort:
        payload["reasoning"] = {"effort": effort}
    if request.get("tools"):
        payload["tools"] = [dict(type="function", name=t["name"],
            description=t.get("description", ""), parameters=t["input_schema"], strict=False)
            for t in request["tools"]]
        payload["parallel_tool_calls"] = False
        if request.get("tool_choice", {}).get("type") == "none":
            payload["tool_choice"] = "none"
    return adapt(await client().responses.create(**payload))
