import logging
from pathlib import Path

from app.modules.dialog import openai_transport

from app.core.config import settings
from app.modules.ops import journal
from app.modules.ops.journal import watch

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
_BASE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_DIALOG_REPORT_PROMPT_PATH = Path(__file__).parent / "prompts" / "dialog_report_prompt.md"
_DIALOG_REPORT_PROMPT = _DIALOG_REPORT_PROMPT_PATH.read_text(encoding="utf-8")

async def _create(operation: str, **request):
    """OpenAI only. Filename and block interface retained for callers."""
    response = await openai_transport.create(**request)
    _log_cache(operation, response)
    journal.note_llm_usage(operation, request.get("model", ""), response)
    return response


def _system_blocks(system: str, cache_upto: int | None):
    """Системный промпт — блоками, с меткой кэша в конце неизменной части.

    Кэш Anthropic — совпадение начала запроса: инструменты, потом система,
    потом переписка. Неизменная часть (базовый промпт, каталог) одинакова у
    всех клиентов на одном этапе, и её повторное чтение из кэша в 10 раз
    дешевле. Что про этого клиента (черновик, покупки, этап) — после метки:
    оно меняется каждый ход, и кэш на нём только платил бы за запись.
    Текст тот же, что без кэша, — меняется лишь деление на блоки.

    `cache_upto` — где кончается неизменная часть; None — весь промпт
    неизменный (ассистент Ops).
    """
    if not settings.anthropic_prompt_cache or not system:
        return system
    if cache_upto is None or cache_upto >= len(system):
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if cache_upto <= 0:
        return system
    blocks = [{"type": "text", "text": system[:cache_upto], "cache_control": {"type": "ephemeral"}}]
    if system[cache_upto:].strip():
        blocks.append({"type": "text", "text": system[cache_upto:]})
    return blocks


def _log_cache(operation: str, response) -> None:
    usage = getattr(response, "usage", None)
    read = getattr(usage, "cache_read_input_tokens", None)
    written = getattr(usage, "cache_creation_input_tokens", None)
    if read or written:
        logger.info("Claude (%s): вход %s, из кэша %s, в кэш %s, выход %s", operation,
                    getattr(usage, "input_tokens", "?"), read or 0, written or 0,
                    getattr(usage, "output_tokens", "?"))


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


@watch("openai", "ответ без инструментов")
async def generate_reply(user_message: str, catalog_context: str = "") -> str:
    system_prompt = _BASE_SYSTEM_PROMPT
    if catalog_context:
        system_prompt += f"\n\nТекущий ассортимент:\n{catalog_context}"

    system_prompt, messages = guard("ответ без инструментов", system_prompt, [{"role": "user", "content": user_message}])
    response = await _create(
        "ответ без инструментов",
        model=settings.openai_model,
        max_tokens=settings.openai_max_tokens,
        system=system_prompt,
        messages=messages,
        **_effort(),
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Claude response contained no text block")


@watch("openai", "мини-отчёт по диалогу")
async def generate_dialog_report(transcript: str) -> str:
    # Вызывается из задачи по таймеру, а не из обработки сообщения, поэтому
    # спешить некуда: восьмисекундный бюджет вебхука VK здесь ни при чём.
    system, messages = guard("мини-отчёт по диалогу", _DIALOG_REPORT_PROMPT, [{"role": "user", "content": transcript}])
    response = await _create(
        "мини-отчёт по диалогу",
        model=settings.openai_model,
        # Отчёт короткий, но размышление идёт в тот же лимит: при 400 его
        # могло не хватить даже на одну фразу.
        max_tokens=2000,
        system=system,
        messages=messages,
        output_config={"effort": "low"},
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Claude response contained no text block")


def _well_formed(messages: list[dict]) -> list[dict]:
    """История без пустых реплик: строку из одних пробелов API не принимает (400),
    и клиент получал «техническую заминку» вместо ответа."""
    return [
        message for message in messages
        if not (isinstance(message.get("content"), str) and not message["content"].strip())
    ]


@watch("openai", "ход диалога")
async def converse(messages: list[dict], system_prompt: str, tools: list[dict], *, words_only: bool = False,
                   cache_upto: int | None = 0):
    # `words_only` — модель обязана ответить словами (последний круг хода).
    # Инструменты при этом остаются в запросе, вызывать их запрещает
    # tool_choice: убрать их нельзя, если в истории хода уже есть вызовы
    # инструментов, — API отвечает 400, и клиент получал «техническую
    # заминку» вместо ответа (05.10.2026).
    extra: dict = {}
    if tools:
        extra["tools"] = tools
        if words_only:
            extra["tool_choice"] = {"type": "none"}
    messages = _well_formed(messages)
    # Деление на блоки — до последнего рубежа: он чистит каждый блок, а
    # граница неизменной части считается по исходному тексту.
    system_prompt, messages = guard("ход диалога", _system_blocks(system_prompt, cache_upto), messages)
    response = await _create(
        "ход диалога",
        model=settings.openai_model,
        max_tokens=settings.openai_max_tokens,
        system=system_prompt,
        messages=messages,
        **_effort(),
        **extra,
    )
    if getattr(response, "stop_reason", None) == "max_tokens":
        logger.warning("Claude: ход упёрся в max_tokens (%s, вывод %s токенов)",
                       settings.openai_max_tokens, getattr(response.usage, "output_tokens", "?"))
    return response


@watch("openai", "ассистент Ops")
async def ops_assistant_turn(messages: list[dict], system_prompt: str, tools: list[dict], *, words_only: bool = False):
    """Ход ассистента в чате Ops. Тот же последний рубеж, что у бота.

    Вопросы туда приходят уже очищенными (app/modules/ops/assistant/redact.py);
    guard здесь — на случай, если очистка что-то пропустила.
    """
    extra: dict = {"tools": tools}
    if words_only:
        extra["tool_choice"] = {"type": "none"}
    effort = (settings.ops_assistant_effort or "").strip()
    if effort:
        extra["output_config"] = {"effort": effort}
    # Промпт и инструменты ассистента одинаковы у всех вопросов — кэшируем
    # целиком; переписку внутри вопроса (круги с инструментами) — меткой
    # верхнего уровня: следующий круг читает предыдущий из кэша.
    if settings.anthropic_prompt_cache:
        extra["cache_control"] = {"type": "ephemeral"}
    system_prompt, messages = guard("ассистент Ops", _system_blocks(system_prompt, None), _well_formed(messages))
    response = await _create(
        "ассистент Ops",
        model=settings.ops_assistant_model or settings.openai_model,
        max_tokens=settings.ops_assistant_max_tokens,
        system=system_prompt,
        messages=messages,
        **extra,
    )
    usage = getattr(response, "usage", None)
    logger.info(
        "Ассистент Ops: ход модели, вход %s, выход %s токенов, stop_reason=%s",
        getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"),
        getattr(response, "stop_reason", None),
    )
    return response


def _effort() -> dict:
    """Глубина размышления — параметром, если задана."""
    effort = (settings.openai_effort or "").strip()
    return {"output_config": {"effort": effort}} if effort else {}


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
