import logging
from typing import Any

from app.modules.dialog import vk_client
from app.modules.orders import conversation as orders_conversation

logger = logging.getLogger(__name__)

# In-memory dedup only: resets on restart and won't work across multiple
# server instances. Fine for a single-process MVP; move to Redis/DB later.
_processed_event_ids: set[str] = set()
_MAX_TRACKED_EVENTS = 10_000


def is_duplicate(event_id: str) -> bool:
    if event_id in _processed_event_ids:
        return True
    if len(_processed_event_ids) >= _MAX_TRACKED_EVENTS:
        _processed_event_ids.clear()
    _processed_event_ids.add(event_id)
    return False


async def handle_message_new(message: dict[str, Any]) -> None:
    peer_id = message["peer_id"]
    text = message.get("text", "")
    if not text:
        return

    try:
        reply = await orders_conversation.handle_turn(peer_id, text)
    except Exception:
        logger.exception("Claude generation failed for peer_id=%s", peer_id)
        reply = "Извините, сейчас не получается ответить. Мы скоро вернёмся с ответом."

    await vk_client.send_message(peer_id, reply)
