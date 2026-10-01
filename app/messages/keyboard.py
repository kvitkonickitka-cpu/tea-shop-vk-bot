"""Inline-клавиатуры ВК под сообщениями бота.

Только inline: клавиатура под полем ввода висит, пока её не уберут, и
через день предлагает «Оформить» заказ, которого уже нет. Лимиты — по
документации ВК (dev.vk.com, «Клавиатура» и «Типы кнопок», сверено
29.09.2026): до 10 кнопок, до 6 рядов по 5, подпись до 40 символов,
payload — JSON-строка до 255 символов.

Кнопки ставим, только если приложение клиента их умеет: `client_info` из
последнего message_new хранится в `client_preferences`. Не знаем, что
умеет, — шлём только текст; поэтому рядом с кнопками в тексте всегда есть
подсказка, как ответить словами.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages.models import ClientPreference

logger = logging.getLogger(__name__)

MAX_BUTTONS = 10
MAX_ROWS = 6
MAX_IN_ROW = 5
MAX_LABEL = 40
MAX_PAYLOAD = 255


def _label(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1].rstrip() + "…"


def text_button(label: str, payload: dict, color: str = "secondary") -> dict:
    """Кнопка, нажатие которой приходит сообщением с текстом подписи.

    Основной тип: текст нажатия остаётся в переписке, его видят модель и
    менеджер, а payload говорит коду, что именно нажали.
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > MAX_PAYLOAD:
        raise ValueError(f"payload кнопки длиннее {MAX_PAYLOAD} символов: {raw[:60]}…")
    return {"action": {"type": "text", "label": _label(label), "payload": raw}, "color": color}


def link_button(label: str, link: str) -> dict:
    return {"action": {"type": "open_link", "label": _label(label), "link": link}}


def inline(rows: list[list[dict]]) -> dict | None:
    """Клавиатура из рядов кнопок — в пределах лимитов ВК."""
    rows = [row[:MAX_IN_ROW] for row in rows if row][:MAX_ROWS]
    kept, count = [], 0
    for row in rows:
        room = MAX_BUTTONS - count
        if room <= 0:
            break
        kept.append(row[:room])
        count += len(kept[-1])
    return {"inline": True, "buttons": kept} if kept else None


def _types(keyboard: dict) -> set[str]:
    return {button["action"]["type"] for row in keyboard["buttons"] for button in row}


def supports(client_info: dict | None, keyboard: dict) -> bool:
    """Покажет ли приложение клиента эту клавиатуру."""
    if not client_info or not client_info.get("inline_keyboard"):
        return False
    actions = set(client_info.get("button_actions") or [])
    return _types(keyboard) <= actions


async def remember_client(peer_id: int, client_info: dict | None) -> None:
    """Запомнить, что умеет приложение клиента, — из последнего message_new."""
    if not client_info:
        return
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return
    statement = insert(ClientPreference).values(
        peer_id=peer_id, marketing_opt_out=False, client_info=client_info
    ).on_conflict_do_update(
        index_elements=[ClientPreference.peer_id], set_={"client_info": client_info}
    )
    try:
        async with session_factory() as session:
            await session.execute(statement)
            await session.commit()
    except Exception:
        logger.exception("Не запомнили client_info для peer_id=%s", peer_id)


async def client_info_of(peer_id: int) -> dict | None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return None
    async with session_factory() as session:
        row = await session.get(ClientPreference, peer_id)
    return row.client_info if row is not None else None


async def for_peer(peer_id: int, keyboard: dict | None) -> str | None:
    """JSON клавиатуры для messages.send — или None, если слать только текст."""
    if keyboard is None or not settings.vk_buttons_enabled:
        return None
    try:
        info = await client_info_of(peer_id)
    except Exception:
        logger.exception("Не узнали, что умеет приложение peer_id=%s", peer_id)
        return None
    if not supports(info, keyboard):
        return None
    return json.dumps(keyboard, ensure_ascii=False, separators=(",", ":"))


def parse_payload(raw) -> dict | None:
    """Payload нажатия. Клиент может подменить его руками — проверяем форму."""
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw or "")
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict) or not isinstance(data.get("a"), str):
        return None
    return data
