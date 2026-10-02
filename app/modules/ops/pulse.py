"""Минутный пульс: жив ли бот, жива ли база, что творилось последние 10 минут.

Пульс запускает отдельный таймерный триггер раз в минуту (в поле «Данные» —
`ops-pulse` и токен). Пульс доходит до Monitoring, только если сработал
триггер, поднялся контейнер и отработал код, поэтому «нет пульса три минуты»
и есть алерт «бот не работает». База упала — пульс всё равно уходит, но с
`bot_db_up = 0`.

Что уходит в Monitoring (все метрики DGAUGE, имя — как в алертах):

- `bot_heartbeat` = 1;
- `bot_db_up` — 1/0;
- `bot_turns` — сколько ответов клиентам за окно;
- `bot_response_p95_seconds{stage=total|llm}`, `bot_response_p50_seconds{…}`
  — только если ответы в окне были: нет ответов — нет и задержки;
- `external_api_errors{api=yookassa|cdek|ozon|claude}` — сбои за окно, без
  `validation`; нули отправляются тоже, иначе алерт не вернётся в OK;
- `external_api_validation{api=…}` — отказы из-за ввода клиента, для дашборда.

Перцентиль считает база, а не алерт: функций перцентиля у алертов
Monitoring нет.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.ops import journal, monitoring

logger = logging.getLogger(__name__)

MARK = "ops-pulse"
_DB_TIMEOUT_SECONDS = 3


async def window_stats(session, minutes: int, *, skip_emulation: bool = False) -> dict:
    """Ошибки по сервисам и перцентили ответа за последние `minutes` минут."""
    params = {"minutes": minutes, "emulation": journal.EMULATION}
    emulation = " and coalesce(operation, '') <> :emulation" if skip_emulation else ""
    errors: dict[str, dict[str, int]] = {}
    rows = await session.execute(
        text(
            "select api, error_kind, count(*) from ops_events "
            "where kind = 'error' and at > now() - make_interval(mins => :minutes)"
            f"{emulation} group by api, error_kind"
        ),
        params,
    )
    for api, kind, count in rows.all():
        errors.setdefault(api or "?", {})[kind or "other"] = int(count)

    turn = (
        await session.execute(
            text(
                "select count(*), "
                "percentile_cont(0.95) within group (order by duration_ms), "
                "percentile_cont(0.5) within group (order by duration_ms), "
                "percentile_cont(0.95) within group (order by llm_ms), "
                "percentile_cont(0.5) within group (order by llm_ms) "
                "from ops_events where kind = 'turn' "
                f"and at > now() - make_interval(mins => :minutes){emulation}"
            ),
            params,
        )
    ).one()
    turns = int(turn[0] or 0)
    seconds = lambda value: round(float(value) / 1000, 2) if value is not None else None  # noqa: E731
    return {
        "errors": errors,
        "turns": turns,
        "p95": seconds(turn[1]) if turns else None,
        "p50": seconds(turn[2]) if turns else None,
        "llm_p95": seconds(turn[3]) if turns else None,
        "llm_p50": seconds(turn[4]) if turns else None,
    }


def alerting_errors(errors: dict[str, dict[str, int]], api: str) -> int:
    return sum(count for kind, count in errors.get(api, {}).items() if kind != journal.VALIDATION)


def metrics_for(db_up: bool, stats: dict | None) -> list[dict]:
    gauge = monitoring.gauge
    metrics = [gauge("bot_heartbeat", 1), gauge("bot_db_up", 1 if db_up else 0)]
    if stats is None:
        return metrics
    metrics.append(gauge("bot_turns", stats["turns"]))
    if stats["turns"]:
        metrics += [
            gauge("bot_response_p95_seconds", stats["p95"], stage="total"),
            gauge("bot_response_p50_seconds", stats["p50"], stage="total"),
            gauge("bot_response_p95_seconds", stats["llm_p95"], stage="llm"),
            gauge("bot_response_p50_seconds", stats["llm_p50"], stage="llm"),
        ]
    for api in journal.APIS:
        metrics.append(gauge("external_api_errors", alerting_errors(stats["errors"], api), api=api))
        metrics.append(gauge(
            "external_api_validation",
            stats["errors"].get(api, {}).get(journal.VALIDATION, 0),
            api=api,
        ))
    return metrics


async def _collect() -> tuple[bool, dict | None]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        await session.execute(text("select 1"))
        stats = await window_stats(session, settings.ops_window_minutes)
        await session.execute(text("insert into ops_events (kind) values ('pulse')"))
        await session.commit()
    return True, stats


async def run() -> dict:
    """Один пульс. Никогда не бросает: пульс, упавший с ошибкой, — не пульс."""
    flushed = await journal.flush()
    try:
        db_up, stats = await asyncio.wait_for(_collect(), _DB_TIMEOUT_SECONDS)
    except Exception as error:
        logger.warning("Пульс: база не ответила — %s", type(error).__name__)
        db_up, stats = False, None

    metrics = metrics_for(db_up, stats)
    sent = await monitoring.write(metrics)
    result = {
        "db_up": db_up,
        "metrics_sent": sent if monitoring.is_configured() else "YC_FOLDER_ID не задан",
        "flushed": flushed,
    }
    if stats is not None:
        result.update(
            turns=stats["turns"], p95=stats["p95"], llm_p95=stats["llm_p95"],
            errors={api: alerting_errors(stats["errors"], api) for api in journal.APIS},
        )
    logger.info("Пульс: %s", result)
    return result
