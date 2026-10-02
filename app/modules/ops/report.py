"""Ежедневный отчёт в Ops-чат: как бот прожил сутки.

Уходит из тика расписания один раз в день, после `ops_report_hour_msk` по
Москве. Отметка об отправке — в `heartbeats`, так что перезапуск контейнера
второй отчёт за день не пришлёт, а неудачная отправка повторится следующим
тиком.

Чего в отчёте нет и быть не может: имён, телефонов, текстов переписки.
Только числа, названия сервисов и время задач.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core import heartbeat, worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.ops import journal, monitoring, pulse

logger = logging.getLogger(__name__)

HEARTBEAT = "ops-отчёт"

# Задачи по расписанию: подпись и через сколько молчания это уже тревога.
_TASKS = {
    "расписание": ("тик бота (каждые 5 мин)", timedelta(minutes=15)),
    "отчёт о недоставленном менеджеру": ("отчёт о недоставленном", timedelta(hours=26)),
    "cashflow": ("выписка Т-Банка", timedelta(hours=26)),
    "cashflow-backup": ("бэкап finance", timedelta(hours=26)),
}
_DAY = timedelta(hours=24)


def _num(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}".replace(".", ",")


async def collect(now: datetime | None = None) -> dict:
    """Числа за последние сутки."""
    now = now or datetime.now(timezone.utc)
    session_factory = get_session_factory()
    async with session_factory() as session:
        stats = await pulse.window_stats(session, 24 * 60, skip_emulation=True)
        pulses, first_pulse = (
            await session.execute(
                text(
                    "select count(distinct date_trunc('minute', at)) filter "
                    "(where at > now() - interval '24 hours'), min(at) "
                    "from ops_events where kind = 'pulse'"
                )
            )
        ).one()
        dialogs = (
            await session.execute(
                text(
                    "select count(distinct peer_id) from conversation_messages "
                    "where role = 'user' and created_at > now() - interval '24 hours'"
                )
            )
        ).scalar_one()
    tasks = await _task_times()
    expected = None
    if first_pulse is not None:
        first = first_pulse if first_pulse.tzinfo else first_pulse.replace(tzinfo=timezone.utc)
        expected = int(min(_DAY, now - first).total_seconds() // 60) or 1
    disk = await monitoring.read_last(settings.ops_disk_query) if settings.ops_disk_query else None
    return {
        "now": now, "stats": stats, "pulses": int(pulses or 0), "expected": expected,
        "dialogs": int(dialogs or 0), "tasks": tasks, "disk": disk,
    }


async def _task_times() -> dict[str, datetime]:
    from sqlalchemy import select

    from app.core.heartbeat import Heartbeat

    async with get_session_factory()() as session:
        rows = (await session.execute(select(Heartbeat))).scalars().all()
    result = {}
    for row in rows:
        if row.name == HEARTBEAT:
            continue
        moment = row.last_run_at
        result[row.name] = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return result


def render(data: dict) -> str:
    now = data["now"]
    stats = data["stats"]
    start = worktime.to_msk(now - _DAY).strftime("%d.%m %H:%M")
    end = worktime.to_msk(now).strftime("%d.%m %H:%M")
    lines = [f"📊 <b>Сутки бота</b> · {start} – {end} МСК", ""]

    if data["expected"] is None:
        lines.append("<b>Аптайм</b>: пульса ещё не было — минутный триггер не настроен")
    else:
        share = min(100.0, 100 * data["pulses"] / data["expected"])
        mark = "" if share >= 99 else " ⚠️"
        lines.append(
            f"<b>Аптайм</b>: {_num(share)} % ({data['pulses']} из {data['expected']} минут с пульсом){mark}"
        )

    if stats["turns"]:
        lines.append(
            f"<b>Ответ клиенту</b>: p50 {_num(stats['p50'])} с · p95 {_num(stats['p95'])} с "
            f"· ответов {stats['turns']}"
        )
        lines.append(f"  из них Claude: p50 {_num(stats['llm_p50'])} с · p95 {_num(stats['llm_p95'])} с")
    else:
        lines.append("<b>Ответ клиенту</b>: ответов не было")
    lines.append(f"<b>Диалогов</b>: {data['dialogs']}")

    lines += ["", "<b>Ошибки сервисов</b>"]
    errors = stats["errors"]
    validation = []
    for api in journal.APIS:
        kinds = {k: v for k, v in errors.get(api, {}).items() if k != journal.VALIDATION}
        total = sum(kinds.values())
        detail = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
        name = journal.API_NAMES[api]
        lines.append(f"{name} — {total}" + (f" ({detail})" if detail else "") + (" ⚠️" if total else ""))
        if errors.get(api, {}).get(journal.VALIDATION):
            validation.append(f"{name} {errors[api][journal.VALIDATION]}")
    if validation:
        lines.append("Ввод клиентов, не ошибки: " + ", ".join(validation))

    disk = data["disk"]
    if disk is None:
        lines += ["", "<b>Диск ВМ с базой</b>: нет данных"]
    else:
        mark = " 🔴" if disk >= 90 else " ⚠️" if disk >= 80 else ""
        lines += ["", f"<b>Диск ВМ с базой</b>: {_num(disk, 0)} %{mark}"]

    lines += ["", "<b>Задачи по расписанию</b>"]
    if not data["tasks"]:
        lines.append("отметок нет")
    for name, moment in sorted(data["tasks"].items(), key=lambda item: item[0]):
        label, limit = _TASKS.get(name, (name, timedelta(hours=26)))
        late = now - moment > limit
        when = worktime.to_msk(moment).strftime("%d.%m %H:%M")
        lines.append(f"{html.escape(label)} — {when}" + (" ⚠️ давно не было" if late else " ✅"))
    return "\n".join(lines)


async def _cleanup() -> None:
    async with get_session_factory()() as session:
        await session.execute(
            text("delete from ops_events where at < now() - make_interval(days => :days)"),
            {"days": settings.ops_events_keep_days},
        )
        await session.commit()


async def send(*, force: bool = False, now: datetime | None = None) -> dict:
    """Отправить отчёт, если пора. `force` — сейчас и без отметки (проверка)."""
    if not settings.telegram_ops_chat_id:
        return {"skipped": "TELEGRAM_OPS_CHAT_ID не задан"}
    now = now or datetime.now(timezone.utc)
    if not force:
        local = worktime.to_msk(now)
        if local.hour < settings.ops_report_hour_msk:
            return {"skipped": "рано"}
        last = await heartbeat.last_run(HEARTBEAT)
        if last is not None and worktime.to_msk(last).date() == local.date():
            return {"skipped": "сегодня уже отправлен"}

    from app.modules.dialog import telegram_client

    await journal.flush()
    text_ = render(await collect(now))
    await telegram_client.send_message(text_, chat_id=settings.telegram_ops_chat_id)
    if not force:
        await heartbeat.note(HEARTBEAT)
        try:
            await _cleanup()
        except Exception:
            logger.exception("Не почистили старые записи ops_events")
    return {"sent": True, "text": text_}
