from sqlalchemy import select

from app.core.database import get_session_factory
from app.modules.dialog.models import ConversationMessage

_MAX_HISTORY_MESSAGES = 20

# Резервное хранилище на случай, если DATABASE_URL не настроен (например,
# локальная разработка) — переживёт только до перезапуска процесса.
_fallback_histories: dict[int, list[dict]] = {}


async def get_history(peer_id: int) -> list[dict]:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return list(_fallback_histories.get(peer_id, []))

    async with session_factory() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.peer_id == peer_id)
            .order_by(ConversationMessage.id.desc())
            .limit(_MAX_HISTORY_MESSAGES)
        )
        rows = list(reversed(result.scalars().all()))
        return [{"role": row.role, "content": row.content} for row in rows]


async def append_exchange(peer_id: int, user_text: str, assistant_text: str) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        history = _fallback_histories.setdefault(peer_id, [])
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": assistant_text})
        if len(history) > _MAX_HISTORY_MESSAGES:
            del history[: len(history) - _MAX_HISTORY_MESSAGES]
        return

    async with session_factory() as session:
        session.add_all(
            [
                ConversationMessage(peer_id=peer_id, role="user", content=user_text),
                ConversationMessage(peer_id=peer_id, role="assistant", content=assistant_text),
            ]
        )
        await session.commit()
