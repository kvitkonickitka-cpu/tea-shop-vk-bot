from sqlalchemy import select

from app.core.database import get_session_factory
from app.modules.dialog.models import Conversation, ConversationMessage

_MAX_HISTORY_MESSAGES = 20

# Автор реплики с ролью assistant. NULL/AUTHOR_BOT — сам бот, AUTHOR_MANAGER —
# живой менеджер, зашедший в диалог руками.
AUTHOR_BOT = "bot"
AUTHOR_MANAGER = "manager"

# В API Claude ролей всего две, отдельной «менеджерской» нет. Поэтому автора
# показываем модели прямо в тексте реплики: иначе она читает чужие слова как
# свои и, например, считает, что это она обещала клиенту перезвонить.
_MANAGER_MARKER = "[ответ менеджера] "


def mark_author(content: str, author: str | None) -> str:
    return f"{_MANAGER_MARKER}{content}" if author == AUTHOR_MANAGER else content

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
        return [{"role": row.role, "content": mark_author(row.content, row.author)} for row in rows]


async def append_message(peer_id: int, role: str, content: str, author: str | None = None) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        # В памяти колонки нет, поэтому автора вписываем прямо в текст —
        # снаружи это неотличимо от того, что делает get_history.
        history = _fallback_histories.setdefault(peer_id, [])
        history.append({"role": role, "content": mark_author(content, author)})
        if len(history) > _MAX_HISTORY_MESSAGES:
            del history[: len(history) - _MAX_HISTORY_MESSAGES]
        return

    async with session_factory() as session:
        conversation = await session.get(Conversation, peer_id)
        if conversation is None:
            conversation = Conversation(peer_id=peer_id, message_count=1)
            session.add(conversation)
        else:
            conversation.message_count += 1

        session.add(ConversationMessage(peer_id=peer_id, role=role, content=content, author=author))
        await session.commit()


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
        conversation = await session.get(Conversation, peer_id)
        if conversation is None:
            conversation = Conversation(peer_id=peer_id, message_count=2)
            session.add(conversation)
        else:
            conversation.message_count += 2

        session.add_all(
            [
                ConversationMessage(peer_id=peer_id, role="user", content=user_text),
                ConversationMessage(peer_id=peer_id, role="assistant", content=assistant_text),
            ]
        )
        await session.commit()
