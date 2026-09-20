from pathlib import Path

from anthropic import AsyncAnthropic

from app.core.config import settings

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
_BASE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_ORDER_NOTIFICATION_PROMPT_PATH = Path(__file__).parent / "prompts" / "order_notification_prompt.md"
_ORDER_NOTIFICATION_PROMPT = _ORDER_NOTIFICATION_PROMPT_PATH.read_text(encoding="utf-8")

_DIALOG_REPORT_PROMPT_PATH = Path(__file__).parent / "prompts" / "dialog_report_prompt.md"
_DIALOG_REPORT_PROMPT = _DIALOG_REPORT_PROMPT_PATH.read_text(encoding="utf-8")

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


async def generate_dialog_report(transcript: str) -> str:
    # Вызывается из задачи по таймеру, а не из обработки сообщения, поэтому
    # спешить некуда: восьмисекундный бюджет вебхука VK здесь ни при чём.
    response = await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=400,
        system=_DIALOG_REPORT_PROMPT,
        messages=[{"role": "user", "content": transcript}],
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


def extract_text(response, default: str | None = None) -> str:
    for block in response.content:
        if block.type == "text":
            return block.text
    # Ответ без текста — законный случай: модель может вернуть один вызов
    # инструмента и ничего больше. Бросать здесь значит превращать это в
    # извинение перед клиентом, поэтому вызывающий может дать запасной текст.
    if default is not None:
        return default
    raise ValueError("Claude response contained no text block")
