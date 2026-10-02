"""Подробности сбоев в Ops-чат: что именно сломалось, а не только сколько.

Алерт Monitoring говорит «у ЮKassa 1 сбой за 10 минут» — и всё: в шаблон
аннотации подставляется только число. Операцию, код ответа и номер заказа
знает журнал, поэтому пульс сам досылает подробность нашим ботом:

    🔎 ЮKassa — 1 сбой
    00:24 · GET /payments/{id} — HTTP 404, сервис отклонил запрос · заказ #12

Алерт при этом остаётся главным: он придёт, даже если бот лежит целиком, а
подробность — дополнение, которое приходит, пока бот жив.

Против спама: одно сообщение на сервис не чаще раза в `COOLDOWN`; сбои,
пришедшие за это время, войдут в следующее. Сбой отмечается `notified_at`
условным UPDATE до отправки — два пульса одновременно одну строку не
пошлют. Отправка не удалась — отметка снимается, пошлёт следующий пульс.
Сбои старше `FRESH` не досылаются: после простоя важна свежая картина, а не
пачка вчерашнего.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core import heartbeat, worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.ops import journal

logger = logging.getLogger(__name__)

COOLDOWN = timedelta(minutes=10)
FRESH = timedelta(minutes=30)
HEARTBEAT_PREFIX = "ops-подробности:"
_SHOWN = 5


def _what(kind: str, status: int | None) -> str:
    code = f"HTTP {status}, " if status else ""
    return {
        "timeout": "таймаут — сервис не ответил",
        "network": "нет связи с сервисом",
        "auth": f"{code}отказ в доступе — похоже, истёк или сменился ключ",
        "http_5xx": f"{code}сбой на стороне сервиса",
        "http_4xx": f"{code}сервис отклонил запрос",
    }.get(kind, f"{code}ошибка обработки, подробности в логах")


def _failures(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} сбой"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} сбоя"
    return f"{count} сбоев"


def render(api: str, rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda row: row["at"])
    name = journal.API_NAMES.get(api, api)
    emulated = all(row["operation"] == journal.EMULATION for row in rows)
    lines = [f"🔎 <b>{html.escape(name)}</b> — {_failures(len(rows))}" + (" (эмуляция)" if emulated else "")]
    for row in rows[-_SHOWN:]:
        line = (
            f"{worktime.to_msk(row['at']).strftime('%H:%M')} · "
            f"{html.escape(row['operation'] or '?')} — {_what(row['error_kind'], row['http_status'])}"
        )
        if row["order_id"]:
            line += f" · заказ #{row['order_id']}"
        lines.append(line)
    if len(rows) > _SHOWN:
        lines.append(f"и ещё {len(rows) - _SHOWN} раньше")
    orders = sorted({row["order_id"] for row in rows if row["order_id"]})
    if len(orders) > 1:
        lines.append("Заказы: " + ", ".join(f"#{order}" for order in orders))
    lines.append(f'Логи: <code>json_payload.service = "{html.escape(api)}"</code>')
    return "\n".join(lines)


async def _pending(session) -> list[dict]:
    result = await session.execute(
        text(
            "select id, at, api, operation, error_kind, http_status, order_id from ops_events "
            "where kind = 'error' and error_kind <> :validation and notified_at is null "
            "and at > now() - make_interval(mins => :fresh) order by at"
        ),
        {"validation": journal.VALIDATION, "fresh": int(FRESH.total_seconds() // 60)},
    )
    return [dict(row._mapping) for row in result]


async def _claim(session, ids: list[int]) -> list[int]:
    result = await session.execute(
        text(
            "update ops_events set notified_at = now() "
            "where id = any(:ids) and notified_at is null returning id"
        ),
        {"ids": ids},
    )
    claimed = [row[0] for row in result]
    await session.commit()
    return claimed


async def _release(ids: list[int]) -> None:
    async with get_session_factory()() as session:
        await session.execute(
            text("update ops_events set notified_at = null where id = any(:ids)"), {"ids": ids}
        )
        await session.commit()


async def send_pending(now: datetime | None = None) -> dict:
    """Дослать подробности новых сбоев. Ошибки наружу не отдаёт — их ловит пульс."""
    if not settings.telegram_ops_chat_id:
        return {"skipped": "TELEGRAM_OPS_CHAT_ID не задан"}
    from app.modules.dialog import telegram_client

    now = now or datetime.now(timezone.utc)
    sent: dict[str, int] = {}
    async with get_session_factory()() as session:
        pending = await _pending(session)
        by_api: dict[str, list[dict]] = {}
        for row in pending:
            by_api.setdefault(row["api"] or "?", []).append(row)

        for api, rows in by_api.items():
            last = await heartbeat.last_run(HEARTBEAT_PREFIX + api)
            if last is not None and now - last < COOLDOWN:
                continue
            claimed = set(await _claim(session, [row["id"] for row in rows]))
            rows = [row for row in rows if row["id"] in claimed]
            if not rows:
                continue
            try:
                await telegram_client.send_message(
                    render(api, rows), chat_id=settings.telegram_ops_chat_id
                )
            except Exception as error:
                await _release([row["id"] for row in rows])
                logger.warning("Подробность сбоев %s не ушла в Ops: %s", api, type(error).__name__)
                continue
            await heartbeat.note(HEARTBEAT_PREFIX + api)
            sent[api] = len(rows)
    return {"sent": sent}
