import logging
from pathlib import Path

from anthropic import AsyncAnthropic

from app.core.config import settings
from app.modules.ops import journal
from app.modules.ops.journal import watch

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
_BASE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_DIALOG_REPORT_PROMPT_PATH = Path(__file__).parent / "prompts" / "dialog_report_prompt.md"
_DIALOG_REPORT_PROMPT = _DIALOG_REPORT_PROMPT_PATH.read_text(encoding="utf-8")

_client = AsyncAnthropic(
    api_key=settings.anthropic_api_key,
    base_url=settings.anthropic_base_url or None,
)


def _scrub_value(value, found: list[int]):
    if isinstance(value, str):
        from app import privacy

        clean, count = privacy.scrub(value)
        found[0] += count
        return clean
    if isinstance(value, list):
        return [_scrub_value(item, found) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "image":
            return value  # снимки уходят как есть — остаточный риск, описан в документе
        return {key: _scrub_value(item, found) for key, item in value.items()}
    # Блоки ответа модели (объекты SDK) — её собственный текст, он уже у Anthropic.
    return value


def guard(operation: str, system: str, messages: list[dict]) -> tuple[str, list[dict]]:
    """Последний рубеж: телефон или почта в запросе — на [REDACTED].

    Основной механизм — метки (`app/privacy`); если сюда всё же что-то
    дошло, значит метки где-то пропустили. Значение в лог не пишем, а
    событие кладём в журнал Ops — утечка видна в ежедневном отчёте.
    """
    from app import privacy

    if not privacy.is_enabled():
        return system, messages
    found = [0]
    system = _scrub_value(system, found)
    messages = _scrub_value(messages, found)
    if found[0]:
        logger.warning("Claude (%s): в запросе нашлись телефоны или почты (%d) — заменили на [REDACTED]",
                       operation, found[0])
        journal.note_pii_redacted(operation, found[0])
    return system, messages


@watch("claude", "ответ без инструментов")
async def generate_reply(user_message: str, catalog_context: str = "") -> str:
    system_prompt = _BASE_SYSTEM_PROMPT
    if catalog_context:
        system_prompt += f"\n\nТекущий ассортимент:\n{catalog_context}"

    system_prompt, messages = guard("ответ без инструментов", system_prompt, [{"role": "user", "content": user_message}])
    response = await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=system_prompt,
        messages=messages,
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Claude response contained no text block")


@watch("claude", "мини-отчёт по диалогу")
async def generate_dialog_report(transcript: str) -> str:
    # Вызывается из задачи по таймеру, а не из обработки сообщения, поэтому
    # спешить некуда: восьмисекундный бюджет вебхука VK здесь ни при чём.
    system, messages = guard("мини-отчёт по диалогу", _DIALOG_REPORT_PROMPT, [{"role": "user", "content": transcript}])
    response = await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=400,
        system=system,
        messages=messages,
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Claude response contained no text block")


@watch("claude", "ход диалога")
async def converse(messages: list[dict], system_prompt: str, tools: list[dict]):
    # Пустой список инструментов не передаём вовсе: так вызывается последний
    # круг хода, когда модель обязана ответить словами, а не просить ещё
    # одно действие.
    extra = {"tools": tools} if tools else {}
    system_prompt, messages = guard("ход диалога", system_prompt, messages)
    return await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=system_prompt,
        messages=messages,
        **extra,
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
