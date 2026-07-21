from app.core.database import get_session_factory
from app.modules.dialog.models import EscalationState

# Резервное хранилище на случай, если DATABASE_URL не настроен — переживёт
# только до перезапуска процесса.
_fallback: dict[int, bool] = {}


async def is_open(peer_id: int) -> bool:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return _fallback.get(peer_id, False)

    async with session_factory() as session:
        row = await session.get(EscalationState, peer_id)
        return row.is_open if row is not None else False


async def mark_open(peer_id: int) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        _fallback[peer_id] = True
        return

    async with session_factory() as session:
        row = await session.get(EscalationState, peer_id)
        if row is None:
            row = EscalationState(peer_id=peer_id, is_open=True)
            session.add(row)
        else:
            row.is_open = True
        await session.commit()


async def mark_resolved(peer_id: int) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        _fallback[peer_id] = False
        return

    async with session_factory() as session:
        row = await session.get(EscalationState, peer_id)
        if row is None:
            row = EscalationState(peer_id=peer_id, is_open=False)
            session.add(row)
        else:
            row.is_open = False
        await session.commit()
