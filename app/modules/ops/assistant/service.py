"""Ассистент в чате Ops: отвечает на вопросы по тревогам, когда его зовут.

Зовут двумя способами: отметить бота (@бот вопрос) или ответить на его
сообщение. Всё остальное в чате — и всё из других чатов, где сидит тот же
бот (заказы, эскалации, отчёты), — не читаем и в модель не отправляем.
Фильтр — в коде: Telegram доставляет боту сообщения и из других чатов.

Модель не видит данных клиентов. Вопрос и сообщение, на которое ответили,
проходят redact.clean; данные модель берёт только инструментами
(tools.py), а они отдают служебные поля и идентификаторы. Перед отправкой
в Anthropic стоит ещё последний рубеж claude_client.guard.

Тексты вопросов и ответов в лог не пишем — только кто спросил (Telegram ID),
сколько скрыто, сколько вызовов инструментов и время.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import privacy
from app.core.config import settings
from app.modules.dialog import claude_client, telegram_client
from app.modules.ops.assistant import redact, tools

logger = logging.getLogger(__name__)

PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
MSK = ZoneInfo("Europe/Moscow")

# Разговор продолжается ответом на сообщение ассистента: помним цепочку
# полчаса. Только тексты вопросов (уже очищенные) и ответов.
_THREAD_TTL_SECONDS = 30 * 60
_THREAD_MAX = 200
_TELEGRAM_LIMIT = 3800
_CONTEXT_CHARS = 3000

_threads: dict[int, dict] = {}
_tasks: set[asyncio.Task] = set()
_day: tuple[str, int] = ("", 0)


def _chat_matches(chat_id) -> bool:
    return bool(settings.telegram_ops_chat_id) and str(chat_id) == str(settings.telegram_ops_chat_id).strip()


def _allowed(user_id) -> bool:
    raw = (settings.ops_assistant_allowed_tg_ids or "").replace(" ", "")
    if not raw:
        return True
    return str(user_id) in set(raw.split(","))


def addressed(message: dict, me: dict) -> bool:
    """Позвали ли ассистента: отметили или ответили на его сообщение."""
    reply = message.get("reply_to_message") or {}
    if (reply.get("from") or {}).get("id") == me.get("id"):
        return True
    username = (me.get("username") or "").casefold()
    text = message.get("text") or ""
    for entity in message.get("entities") or []:
        if entity.get("type") == "mention" and username:
            # Смещения у Telegram — в единицах UTF-16: эмодзи перед упоминанием
            # занимает две, и срез по символам Python промахивался.
            utf16 = text.encode("utf-16-le")
            start, length = entity["offset"] * 2, entity["length"] * 2
            mention = utf16[start:start + length].decode("utf-16-le", errors="ignore")
            if mention.casefold() == f"@{username}":
                return True
        if entity.get("type") == "text_mention" and (entity.get("user") or {}).get("id") == me.get("id"):
            return True
    return False


def question_text(message: dict, me: dict) -> str:
    text = message.get("text") or ""
    username = me.get("username") or ""
    if username:
        text = re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE)
    return text.strip()


def _stale(message: dict) -> bool:
    sent = message.get("date")
    if not sent:
        return False
    return time.time() - sent > settings.ops_assistant_stale_minutes * 60


def _under_daily_limit() -> bool:
    global _day
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    day, count = _day if _day[0] == today else (today, 0)
    if count >= settings.ops_assistant_daily_limit:
        _day = (day, count)
        return False
    _day = (day, count + 1)
    return True


def _forget_old_threads() -> None:
    now = time.monotonic()
    for key in [k for k, v in _threads.items() if now - v["at"] > _THREAD_TTL_SECONDS]:
        _threads.pop(key, None)
    while len(_threads) > _THREAD_MAX:
        _threads.pop(next(iter(_threads)))


def handle_update(update: dict, me: dict) -> bool:
    """Решить, наш ли это вопрос, и ответить в фоне. True — взяли в работу."""
    message = update.get("message") or {}
    if not message or not _chat_matches((message.get("chat") or {}).get("id")):
        return False
    sender = message.get("from") or {}
    if sender.get("is_bot") or not addressed(message, me):
        return False
    if not _allowed(sender.get("id")):
        logger.info("Ассистент Ops: вопрос от tg_id=%s не из списка разрешённых — молчим", sender.get("id"))
        return False
    if _stale(message):
        logger.info("Ассистент Ops: вопрос старше %s мин — пропускаем", settings.ops_assistant_stale_minutes)
        return False
    task = asyncio.create_task(_answer_safely(message, me))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def _keep_typing(chat_id: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await telegram_client.typing(chat_id)
        except Exception:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            pass


async def _answer_safely(message: dict, me: dict) -> None:
    chat_id = str(message["chat"]["id"])
    if not _under_daily_limit():
        await _send(chat_id, message["message_id"],
                    f"На сегодня лимит вопросов исчерпан ({settings.ops_assistant_daily_limit}). Завтра отвечу снова.")
        return
    stop = asyncio.Event()
    typing = asyncio.create_task(_keep_typing(chat_id, stop))
    started = time.monotonic()
    try:
        result = await answer(message, me)
    except Exception:
        logger.exception("Ассистент Ops: не получилось ответить")
        result = {"text": "Не получилось ответить: сбой при обращении к модели или базе. Подробности — в логе бота.",
                  "hidden": 0, "tool_calls": 0}
    finally:
        stop.set()
        await typing
    seconds = time.monotonic() - started
    footer = f"⏱ {seconds:.0f} с"
    if result["hidden"]:
        footer += f" · скрыто данных клиента: {result['hidden']}"
    sent = await _send(chat_id, message["message_id"], result["text"], footer=footer)
    logger.info(
        "Ассистент Ops: ответил tg_id=%s, скрыто %s, инструментов %s, %.1f с",
        (message.get("from") or {}).get("id"), result["hidden"], result["tool_calls"], seconds,
    )
    if sent and result.get("thread") is not None:
        _forget_old_threads()
        _threads[sent["message_id"]] = {"at": time.monotonic(), **result["thread"]}


def _to_html(text: str) -> str:
    escaped = html.escape(text.strip())
    # Команды в обратных кавычках — моноширинным: их копируют на ВМ.
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)


async def _send(chat_id: str, reply_to: int, text: str, *, footer: str = "") -> dict | None:
    body = _to_html(text)
    if len(body) > _TELEGRAM_LIMIT:
        body = body[:_TELEGRAM_LIMIT].rsplit("\n", 1)[0] + "\n…"
    if footer:
        body += f"\n\n<i>{html.escape(footer)}</i>"
    try:
        return await telegram_client.reply(body, chat_id=chat_id, reply_to=reply_to)
    except Exception:
        logger.exception("Ассистент Ops: ответ в Telegram не ушёл")
        return None


def _first_message(question: str, context: str) -> str:
    parts = []
    if context:
        parts.append(f"Сообщение в чате, на которое ответили:\n<<<\n{context}\n>>>")
    parts.append(f"Вопрос: {question or 'Объясни это сообщение: что случилось и что делать?'}")
    return "\n\n".join(parts)


async def answer(message: dict, me: dict) -> dict:
    """Собрать запрос, пройти круги с инструментами, вернуть текст ответа."""
    stop = await privacy.stop_list()
    hidden = 0

    question, found = redact.clean(question_text(message, me), stop)
    hidden += found

    reply = message.get("reply_to_message") or {}
    history: list[dict] = []
    context = ""
    thread = _threads.get(reply.get("message_id")) if (reply.get("from") or {}).get("id") == me.get("id") else None
    if thread:
        context = thread["context"]
        for asked, said in thread["turns"]:
            history += [{"role": "user", "content": asked}, {"role": "assistant", "content": said}]
    elif reply:
        context, found = redact.clean((reply.get("text") or reply.get("caption") or "")[:_CONTEXT_CHARS], stop)
        hidden += found

    first = _first_message(question, context) if not history else (question or "Продолжи.")
    messages = [*history, {"role": "user", "content": first}]

    calls = 0
    text = ""
    while True:
        words_only = calls >= settings.ops_assistant_max_tool_calls
        response = await claude_client.ops_assistant_turn(messages, PROMPT, tools.TOOLS, words_only=words_only)
        if getattr(response, "stop_reason", None) == "refusal":
            text = "Модель отказалась отвечать на этот вопрос. Попробуйте переформулировать."
            break
        uses = [block for block in response.content if block.type == "tool_use"]
        if getattr(response, "stop_reason", None) != "tool_use" or not uses or words_only:
            text = claude_client.extract_text(response, default="")
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in uses:
            calls += 1
            data = await tools.run(block.name, block.input or {})
            counter = [0]
            data = redact.clean_data(data, stop, counter)
            hidden += counter[0]
            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(data, ensure_ascii=False, default=str),
            })
        messages.append({"role": "user", "content": results})

    text = text.strip() or "Не нашёл, что ответить: модель вернула пустой ответ."
    # Модель видела только очищенное; телефоны и почты в ответе — значит,
    # что-то просочилось, и в чат это не уйдёт.
    text, leaked = privacy.scrub(text)
    if leaked:
        logger.warning("Ассистент Ops: в ответе модели нашлись телефоны или почты (%s) — сняли", leaked)
    turns = [*(thread["turns"] if thread else []), (first, text)][-6:]
    return {
        "text": text, "hidden": hidden, "tool_calls": calls,
        "thread": {"context": context, "turns": turns},
    }
