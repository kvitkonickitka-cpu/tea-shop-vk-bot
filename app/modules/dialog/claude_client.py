from pathlib import Path

from anthropic import AsyncAnthropic

from app.core.config import settings

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
_BASE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def generate_reply(user_message: str, catalog_context: str = "") -> str:
    system_prompt = _BASE_SYSTEM_PROMPT
    if catalog_context:
        system_prompt += f"\n\nТекущий ассортимент:\n{catalog_context}"

    response = await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Claude response contained no text block")
