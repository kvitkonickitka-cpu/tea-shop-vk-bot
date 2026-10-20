"""Сессии диалога и источник клиента (first touch / last touch).

Сессия открывается сообщением клиента: первым вообще, первым после суток
тишины или первым после оплаты (`close` по `payment_succeeded`). Её номер —
`dialog_id` в журнале воронки и в расходе Claude: по нему считаются рубли на
диалог и время между шагами воронки.

Источник:
- `ad:<ref>` — клиент пришёл по ссылке vk.me/<сообщество>?ref=<метка>;
- `referral:<код>` — по ссылке друга (`?ref=r_<код>`);
- `organic` — без метки;
- `own` — «свои», ручная метка командой `/own <peer_id>` в чате Ops; в
  представлениях она перекрывает и первый источник, и источник сессий.

Первый источник клиента (`clients.first_source`) ставится один раз и не
перезаписывается; у каждой сессии — свой (`dialog_sessions.source`).
Метки — служебные строки из ссылки, персональных данных в них нет.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update

from app.core.database import get_session_factory
from app.modules.dialog.models import DialogSession

logger = logging.getLogger(__name__)

GAP = timedelta(hours=24)
ORGANIC = "organic"
OWN = "own"
REFERRAL_PREFIX = "r_"
# Две части ключа рекомендательной блокировки: своё пространство, чтобы не
# пересечься с блокировкой хода на peer_id (dialog/inbound.py).
_LOCK_SPACE = 74_062
_REF_LIMIT = 100
_SAFE_REF = re.compile(r"[^0-9A-Za-zА-Яа-яЁё_.\-]")


def _clean(ref: str) -> str:
    return _SAFE_REF.sub("", ref.strip())[:_REF_LIMIT]


def classify(ref: str | None) -> str:
    """Источник по метке из ссылки."""
    value = _clean(ref or "")
    if not value:
        return ORGANIC
    if value.casefold().startswith(REFERRAL_PREFIX):
        return referral_source(value[len(REFERRAL_PREFIX):])
    return f"ad:{value}"


def referral_source(code: str) -> str:
    """`referral:<код>` — код друга из ссылки; пустой — всё равно рефералка, не реклама."""
    code = _clean(code).upper()
    return f"referral:{code or 'unknown'}"


def _lock_key(peer_id: int) -> int:
    return int(peer_id) % 2_147_483_647


async def touch(peer_id: int, *, ref: str | None = None, ref_source: str | None = None,
                at: datetime | None = None) -> int | None:
    """Сообщение клиента: продлить сессию или открыть новую. Номер сессии или None.

    Ошибка базы — None и строка в логе: ответ клиенту важнее аналитики.
    """
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return None
    now = at or datetime.now(timezone.utc)
    try:
        async with session_factory() as session:
            await session.execute(text("select pg_advisory_xact_lock(:space, :key)"),
                                  {"space": _LOCK_SPACE, "key": _lock_key(peer_id)})
            last = (await session.execute(
                select(DialogSession).where(DialogSession.peer_id == peer_id)
                .order_by(DialogSession.started_at.desc(), DialogSession.id.desc()).limit(1)
            )).scalar_one_or_none()
            if last is not None and last.closed_at is None and now - last.last_message_at < GAP:
                last.last_message_at = max(last.last_message_at, now)
                await session.commit()
                return last.id
            fresh = DialogSession(
                peer_id=peer_id, started_at=now, last_message_at=now, source=classify(ref),
                ref=_clean(ref)[:200] if ref else None,
                ref_source=_clean(ref_source)[:200] if ref_source else None,
            )
            session.add(fresh)
            await session.commit()
            return fresh.id
    except Exception:
        logger.exception("Не отметили сессию диалога peer_id=%s", peer_id)
        return None


async def current_id(peer_id: int) -> int | None:
    """Номер последней сессии клиента (без открытия новой)."""
    try:
        async with get_session_factory()() as session:
            return (await session.execute(
                select(DialogSession.id).where(DialogSession.peer_id == peer_id)
                .order_by(DialogSession.started_at.desc(), DialogSession.id.desc()).limit(1)
            )).scalar()
    except Exception:
        logger.warning("Не нашли сессию диалога peer_id=%s", peer_id, exc_info=True)
        return None


def current_id_sql(peer_id: int):
    """Подзапрос «последняя сессия клиента» — чтобы проставить dialog_id в той же вставке."""
    return (
        select(DialogSession.id).where(DialogSession.peer_id == peer_id)
        .order_by(DialogSession.started_at.desc(), DialogSession.id.desc()).limit(1)
        .scalar_subquery()
    )


async def close(peer_id: int, reason: str, *, at: datetime | None = None) -> None:
    """Закрыть открытую сессию: следующее сообщение клиента начнёт новую."""
    try:
        async with get_session_factory()() as session:
            await session.execute(
                update(DialogSession)
                .where(DialogSession.id == current_id_sql(peer_id), DialogSession.closed_at.is_(None))
                .values(closed_at=at or datetime.now(timezone.utc), close_reason=reason)
            )
            await session.commit()
    except Exception:
        logger.exception("Не закрыли сессию диалога peer_id=%s", peer_id)


async def _claim(column: str, session_id: int, at: datetime | None) -> bool:
    """Поставить отметку один раз: True — поставил этот вызов."""
    try:
        async with get_session_factory()() as session:
            done = await session.execute(
                text(f"update dialog_sessions set {column} = :at where id = :id and {column} is null"),
                {"at": at or datetime.now(timezone.utc), "id": session_id},
            )
            await session.commit()
            return bool(done.rowcount)
    except Exception:
        logger.exception("Не отметили %s у сессии %s", column, session_id)
        return False


async def claim_consult_started(session_id: int, at: datetime | None = None) -> bool:
    return await _claim("consult_started_at", session_id, at)


async def claim_consult_qualified(session_id: int, at: datetime | None = None) -> bool:
    """Квалификация — только у сессии, где консультация уже начата."""
    try:
        async with get_session_factory()() as session:
            started = (await session.execute(
                select(DialogSession.consult_started_at).where(DialogSession.id == session_id)
            )).scalar()
    except Exception:
        logger.exception("Не прочитали сессию %s", session_id)
        return False
    if started is None:
        return False
    return await _claim("consult_qualified_at", session_id, at)


async def get(session_id: int) -> DialogSession | None:
    try:
        async with get_session_factory()() as session:
            return await session.get(DialogSession, session_id)
    except Exception:
        logger.warning("Не прочитали сессию %s", session_id, exc_info=True)
        return None


async def mark_own(peer_id: int, *, by: str, own: bool = True) -> bool:
    """Пометить клиента «свои» (или снять). False — такого клиента нет."""
    async with get_session_factory()() as session:
        done = await session.execute(
            text("update clients set own_at = case when :own then coalesce(own_at, now()) end, "
                 "own_by = case when :own then :by end where peer_id = :p"),
            {"own": own, "by": by[:120], "p": peer_id},
        )
        await session.commit()
    if done.rowcount:
        logger.info("peer_id=%s: метка «свои» %s (%s)", peer_id, "поставлена" if own else "снята", by)
    return bool(done.rowcount)
