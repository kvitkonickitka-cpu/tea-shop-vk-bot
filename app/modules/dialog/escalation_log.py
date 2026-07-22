from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.core.database import get_session_factory
from app.modules.dialog.models import Escalation


async def get_latest_open(peer_id: int) -> Optional[Escalation]:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return None

    async with session_factory() as session:
        result = await session.execute(
            select(Escalation)
            .where(Escalation.peer_id == peer_id, Escalation.resolved_at.is_(None))
            .order_by(Escalation.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def record_escalation(peer_id: int, question: str, reason: str) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        # Без БД просто не ведём статистику — это не критично для работы бота.
        return

    async with session_factory() as session:
        session.add(Escalation(peer_id=peer_id, question=question, reason=reason))
        await session.commit()


async def resolve_latest(peer_id: int, admin_id: int) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return

    async with session_factory() as session:
        result = await session.execute(
            select(Escalation)
            .where(Escalation.peer_id == peer_id, Escalation.resolved_at.is_(None))
            .order_by(Escalation.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.resolved_at = datetime.now(timezone.utc)
            row.resolved_by_admin_id = admin_id
            await session.commit()
