from pathlib import Path

from anthropic import AsyncAnthropic

from app.core.config import settings

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
_BASE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_ORDER_NOTIFICATION_PROMPT_PATH = Path(__file__).parent / "prompts" / "order_notification_prompt.md"
_ORDER_NOTIFICATION_PROMPT = _ORDER_NOTIFICATION_PROMPT_PATH.read_text(encoding="utf-8")

_client = AsyncAnthropic(
    api_key=settings.anthropic_api_key,
    base_url=settings.anthropic_base_url or None,
)


async def generate_reply(user_message: str, catalog_context: str = "") -> str:
    system_prompt = _BASE_SYSTEM_PROMPT
    if catalog_context:
        system_prompt += f"\n\nТекущий ассортимент:\n{catalog_context}"

    response = await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Claude response contained no text block")


async def generate_order_notification(facts: str) -> str:
    response = await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=600,
        system=_ORDER_NOTIFICATION_PROMPT,
        messages=[{"role": "user", "content": facts}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Claude response contained no text block")


async def converse(messages: list[dict], system_prompt: str, tools: list[dict]):
    return await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=system_prompt,
        tools=tools,
        messages=messages,
    )


def extract_text(response) -> str:
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Claude response contained no text block")
