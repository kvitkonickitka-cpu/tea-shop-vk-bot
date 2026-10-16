"""Что ассистент Ops может узнать сам: только служебные данные.

Каждый инструмент — фиксированный SQL с перечнем столбцов, а не «выбери
что хочешь»: так данные клиента не попадут к модели даже по ошибке в
вопросе. Отдаём статусы, времена, суммы, счётчики и идентификаторы (номер
заказа, peer_id, номер платежа, номер отправления). Не отдаём: получателя,
телефон, почту, адреса и пункты, тексты переписки, вопросы менеджеру,
отзывы, `details` заказа целиком.

Тексты ошибок внешних сервисов (`last_error`) отдаём обрезанными и после
очистки (`redact.clean_data` в service.py): перевозчик может вернуть в
ошибке чьё-то имя или телефон.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import client as client_messages

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
_MAX_HOURS = 24 * 14
_ERROR_CHARS = 160


def _t(value) -> str | None:
    """Время по Москве, коротко: модели и людям в чате так понятнее."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(MSK).strftime("%d.%m %H:%M")
    return str(value)


def _hours(value, default: int = 24) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(hours, _MAX_HOURS))


async def _rows(sql: str, **params) -> list[dict]:
    async with get_session_factory()() as session:
        result = await session.execute(text(sql), params)
        return [dict(row._mapping) for row in result]


async def service_errors(hours=24, api=None) -> dict:
    hours = _hours(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    groups = await _rows(
        """
        select api, operation, error_kind, http_status, count(*) as count,
               min(at) as first_at, max(at) as last_at,
               (array_agg(distinct order_id) filter (where order_id is not null))[1:20] as order_ids
        from ops_events
        where kind = 'error' and at >= :since and (cast(:api as text) is null or api = :api)
        group by api, operation, error_kind, http_status
        order by max(at) desc
        limit 40
        """,
        since=since, api=api or None,
    )
    for row in groups:
        row["first_at"], row["last_at"] = _t(row["first_at"]), _t(row["last_at"])
        row["order_ids"] = list(row["order_ids"] or [])
    redacted = await _rows(
        "select operation, count(*) as count from ops_events "
        "where kind = 'pii_redacted' and at >= :since group by operation",
        since=since,
    )
    return {"hours": hours, "errors": groups, "pii_redacted_in_claude_requests": redacted}


async def reply_times(hours=24) -> dict:
    hours = _hours(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = await _rows(
        """
        select count(*) as turns,
               percentile_cont(0.5) within group (order by duration_ms) / 1000.0 as p50_s,
               percentile_cont(0.95) within group (order by duration_ms) / 1000.0 as p95_s,
               max(duration_ms) / 1000.0 as max_s,
               percentile_cont(0.95) within group (order by llm_ms) / 1000.0 as llm_p95_s,
               count(*) filter (where duration_ms > 8000) as over_8s
        from ops_events where kind = 'turn' and at >= :since
        """,
        since=since,
    )
    stats = rows[0] if rows else {}
    return {"hours": hours, **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in stats.items()}}


async def host_load(hours=24) -> dict:
    """Загрузка ВМ и бота по минутам из пульса — и как она связана с ходами."""
    from app.modules.ops import resources

    hours = _hours(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = await _rows(
        """
        with minutes as (
            select date_trunc('minute', at) as minute, data from ops_events
            where kind = 'pulse' and data is not null and at >= :since
        ), turns as (
            select date_trunc('minute', at) as minute, count(*) as turns, max(duration_ms) as slowest_ms
            from ops_events where kind = 'turn' and at >= :since group by 1
        )
        select m.minute, m.data, coalesce(t.turns, 0) as turns, t.slowest_ms
        from minutes m left join turns t using (minute) order by m.minute
        """,
        since=since,
    )
    if not rows:
        return {"hours": hours, "error": "замеров за этот период нет (замеры пишутся с 15.10.2026)",
                "now": resources.sample()}

    def stat(selected, key, pick=max):
        values = [r["data"][key] for r in selected if key in r["data"]]
        return round(pick(values), 1) if values else None

    busy = [r for r in rows if r["turns"]]
    idle = [r for r in rows if not r["turns"]]
    top = sorted(rows, key=lambda r: r["data"].get("cpu_max", 0), reverse=True)[:8]
    total_mb = next((r["data"]["mem_total_mb"] for r in reversed(rows) if "mem_total_mb" in r["data"]), None)
    return {
        "hours": hours, "minutes_measured": len(rows), "cpu_cores": 1, "mem_total_mb": total_mb,
        "whole_period": {
            "cpu_max": stat(rows, "cpu_max"), "steal_max": stat(rows, "steal_max"),
            "bot_cpu_max": stat(rows, "bot_cpu_max"), "load1_max": stat(rows, "load1_max"),
            "mem_available_min_mb": stat(rows, "mem_available_mb", min), "bot_mem_max_mb": stat(rows, "bot_mem_mb"),
            "swap_used_max_mb": stat(rows, "swap_used_mb"),
        },
        "minutes_with_client_turns": {
            "count": len(busy), "turns": sum(r["turns"] for r in busy),
            "cpu_max": stat(busy, "cpu_max"), "bot_cpu_max": stat(busy, "bot_cpu_max"),
            "mem_available_min_mb": stat(busy, "mem_available_mb", min), "bot_mem_max_mb": stat(busy, "bot_mem_mb"),
        },
        "minutes_without_turns": {
            "count": len(idle), "cpu_max": stat(idle, "cpu_max"), "cpu_avg_max": stat(idle, "cpu_avg"),
        },
        "busiest_minutes": [
            {"minute": _t(r["minute"]), "turns": r["turns"],
             "slowest_turn_s": round(r["slowest_ms"] / 1000, 1) if r["slowest_ms"] else None, **r["data"]}
            for r in top
        ],
        "now": resources.sample(),
    }


async def api_latency(hours=24, api=None) -> dict:
    """Сколько ждём ответа внешних сервисов: удачные вызовы из журнала."""
    hours = _hours(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = await _rows(
        """
        select api, operation, count(*) as calls,
               percentile_cont(0.5) within group (order by duration_ms) as p50_ms,
               percentile_cont(0.95) within group (order by duration_ms) as p95_ms,
               max(duration_ms) as max_ms
        from ops_events
        where kind = 'call' and at >= :since and (cast(:api as text) is null or api = :api)
        group by api, operation order by api, count(*) desc
        """,
        since=since, api=api or None,
    )
    for row in rows:
        for key in ("p50_ms", "p95_ms"):
            row[key] = int(row[key]) if row[key] is not None else None
    note = "удачные вызовы пишутся с 15.10.2026; сбои — в service_errors"
    return {"hours": hours, "calls": rows, "note": note}


async def event_queue(hours=24) -> dict:
    """События ВК в своей очереди: сколько, каких, как быстро разобраны.

    Тела событий не отдаём — в них текст сообщения клиента. Только тип,
    время, попытки, класс ошибки и peer_id. Разобранные события таблица
    хранит сутки (queue/local.py), поэтому окно больше суток покажет меньше.
    """
    hours = min(_hours(hours), 48)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    by_type = await _rows(
        """
        select to_char(date_trunc('hour', created_at at time zone 'Europe/Moscow'), 'DD.MM HH24:00') as hour_msk,
               body->>'type' as type, count(*) as events,
               count(*) filter (where done_at is not null) as done,
               count(*) filter (where done_at is null) as waiting,
               count(*) filter (where last_error is not null) as with_error,
               round(avg(extract(epoch from done_at - created_at))::numeric, 1) as avg_seconds_to_done,
               round(max(extract(epoch from done_at - created_at))::numeric, 1) as max_seconds_to_done
        from queued_events where created_at >= :since
        group by 1, 2 order by 1, 2
        """,
        since=since,
    )
    recent = await _rows(
        """
        select id, body->>'type' as type,
               coalesce(body->'object'->'message'->>'peer_id', body->>'peer_id') as peer_id,
               created_at, taken_at, done_at, attempts, last_error
        from queued_events order by id desc limit 15
        """
    )
    return _plain({
        "hours": hours, "retention": "разобранные события хранятся сутки",
        "by_hour_and_type": by_type, "recent": recent,
    })


async def order_status(order_id) -> dict:
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        return {"error": "номер заказа — целое число"}
    orders = await _rows(
        """
        select id, peer_id, status, payment_status, receipt_status, delivery_method,
               items_total, delivery_cost, total, is_test, payment_id, ozon_posting, cdek_uuid,
               carrier_status, created_at, paid_at, reminder_1_sent_at, reminder_2_sent_at,
               packed_at, handed_over_at, at_pickup_at, delivered_at, not_delivered_at,
               carrier_checked_at, settlement_receipt_status, settlement_receipt_sent_at,
               jsonb_array_length(coalesce(items, '[]'::jsonb)) as positions
        from orders where id = :id
        """,
        id=order_id,
    )
    if not orders:
        return {"error": f"заказа {order_id} нет"}
    order = orders[0]
    for key, value in list(order.items()):
        if isinstance(value, datetime):
            order[key] = _t(value)
        elif key in ("items_total", "delivery_cost", "total") and value is not None:
            order[key] = float(value)

    payments = await _rows(
        """
        select payment_id, attempt, status, amount, payment_method, created_at, closed_at,
               refund_id is not null as refunded
        from order_payments where order_id = :id order by attempt, created_at
        """,
        id=order_id,
    )
    notices = await _rows(
        """
        select event_type, created_at, sent_at, attempts, left(last_error, :n) as last_error
        from client_notices where ref = :ref order by created_at
        """,
        ref=client_messages.order_ref(order_id), n=_ERROR_CHARS,
    )
    manager = await _rows(
        """
        select kind, created_at, sent_at, attempts, next_attempt_at, fallback_sent_at,
               left(last_error, :n) as last_error
        from manager_notifications where order_id = :id order by created_at
        """,
        id=order_id, n=_ERROR_CHARS,
    )
    funnel = await _rows(
        "select event, source, created_at from funnel_events where order_id = :id order by created_at limit 60",
        id=order_id,
    )
    errors = await _rows(
        """
        select api, operation, error_kind, http_status, count(*) as count, max(at) as last_at
        from ops_events where kind = 'error' and order_id = :id
        group by api, operation, error_kind, http_status order by max(at) desc
        """,
        id=order_id,
    )
    for row in [*payments, *notices, *manager, *funnel, *errors]:
        for key, value in list(row.items()):
            if isinstance(value, datetime):
                row[key] = _t(value)
            elif key == "amount" and value is not None:
                row[key] = float(value)
    return _plain({
        "order": order, "payment_attempts": payments, "client_messages": notices,
        "manager_notifications": manager, "funnel_events": funnel, "service_errors": errors,
    })


async def client_status(peer_id) -> dict:
    try:
        peer_id = int(peer_id)
    except (TypeError, ValueError):
        return {"error": "peer_id — целое число"}
    conversation = await _rows(
        "select started_at, last_message_at, message_count from conversations where peer_id = :p", p=peer_id,
    )
    client = await _rows(
        "select first_contact_at, ref, ref_source, is_test from clients where peer_id = :p", p=peer_id,
    )
    prefs = await _rows(
        """
        select marketing_opt_out, opted_out_at, unreachable_at, fresh_since
        from client_preferences where peer_id = :p
        """,
        p=peer_id,
    )
    orders = await _rows(
        """
        select id, status, payment_status, delivery_method, total, created_at, paid_at, delivered_at
        from orders where peer_id = :p order by created_at desc limit 10
        """,
        p=peer_id,
    )
    draft = await _rows(
        "select stage, delivery_method, items_total, updated_at from order_drafts where peer_id = :p", p=peer_id,
    )
    # Без текста вопроса и причины: там слова клиента.
    escalations = await _rows(
        """
        select id, created_at, resolved_at, reping_sent_at, client_ping_sent_at
        from escalations where peer_id = :p order by created_at desc limit 10
        """,
        p=peer_id,
    )
    inbound = await _rows(
        """
        select count(*) filter (where done_at is null) as not_processed,
               min(received_at) filter (where done_at is null) as oldest_not_processed,
               max(received_at) as last_received
        from inbound_messages where peer_id = :p
        """,
        p=peer_id,
    )
    data = {
        "conversation": conversation[0] if conversation else None,
        "client": client[0] if client else None,
        "preferences": prefs[0] if prefs else None,
        "orders": orders, "draft": draft[0] if draft else None,
        "escalations": escalations, "inbound": inbound[0] if inbound else None,
    }
    return _plain(data)


async def schedule_status() -> dict:
    from app.core import database

    beats = await _rows("select name, last_run_at, runs from heartbeats order by name")
    now = datetime.now(timezone.utc)
    for row in beats:
        last = row["last_run_at"]
        row["minutes_ago"] = int((now - last).total_seconds() // 60) if last else None
        row["last_run_at"] = _t(last)
    outbox = await _rows(
        """
        select count(*) filter (where sent_at is null and fallback_sent_at is null) as manager_not_sent,
               min(created_at) filter (where sent_at is null and fallback_sent_at is null) as oldest_not_sent,
               count(*) filter (where sent_at is null and attempts >= 10) as manager_gave_up
        from manager_notifications
        """
    )
    notices = await _rows(
        """
        select event_type, count(*) as not_sent from client_notices
        where sent_at is null and created_at >= now() - interval '24 hours'
        group by event_type order by 2 desc
        """
    )
    inbound = await _rows(
        """
        select count(*) as not_processed, min(received_at) as oldest
        from inbound_messages where done_at is null
        """
    )
    event_queue = None
    from app.modules.queue import local as local_queue

    if local_queue.is_enabled():
        try:
            event_queue = await local_queue.stats()
        except Exception:
            event_queue = {"error": "не прочитали"}
    return _plain({
        "revision": settings.app_revision or None,
        "event_queue": event_queue,
        "database": "ok" if database.is_available() else "недоступна",
        "heartbeats": beats,
        "manager_outbox": outbox[0] if outbox else None,
        "client_messages_not_sent_24h": notices,
        "inbound_queue": inbound[0] if inbound else None,
    })


def _plain(value):
    if isinstance(value, datetime):
        return _t(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "is_finite"):  # Decimal
        return float(value)
    return value


TOOLS = [
    {
        "name": "service_errors",
        "description": (
            "Сбои внешних сервисов (yookassa, cdek, ozon, claude, vk) из журнала Ops за последние N часов: "
            "сервис, операция, вид ошибки, HTTP-код, сколько раз, первый и последний раз, номера заказов. "
            "Плюс сколько раз последний рубеж снимал телефоны и почты в запросах к Claude."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Окно в часах, по умолчанию 24, не больше 336."},
                "api": {"type": "string", "description": "Только этот сервис: yookassa, cdek, ozon, claude, vk."},
            },
        },
    },
    {
        "name": "host_load",
        "description": (
            "Загрузка ВМ (1 ядро) и бота по минутам за N часов: процессор ВМ и steal, процессор и память "
            "контейнера бота, свободная память ВМ, swap, load average — среднее и пик внутри минуты. "
            "Отдельно — минуты, когда бот отвечал клиентам, и самые нагруженные минуты с числом ходов. "
            "Плюс замер прямо сейчас."
        ),
        "input_schema": {"type": "object", "properties": {"hours": {"type": "integer"}}},
    },
    {
        "name": "api_latency",
        "description": (
            "Сколько мы ждём ответа внешних сервисов (удачные вызовы): yookassa, cdek, ozon, claude, vk "
            "(отправка сообщения) — по операциям: число вызовов, p50, p95, максимум в мс."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer"}, "api": {"type": "string"}},
        },
    },
    {
        "name": "event_queue",
        "description": (
            "Очередь событий ВК (queued_events) за N часов, до 48: по часам (МСК) и типам — сколько пришло, "
            "разобрано, ждёт, с ошибкой, среднее и максимальное время от прихода до конца разбора в секундах; "
            "плюс 15 последних событий: тип, peer_id, время прихода и разбора, попытки, ошибка. "
            "Тексты сообщений клиентов не отдаются. Разобранные события хранятся сутки."
        ),
        "input_schema": {"type": "object", "properties": {"hours": {"type": "integer"}}},
    },
    {
        "name": "reply_times",
        "description": "Время ответа бота клиентам за N часов: число ходов, p50, p95, максимум, p95 Claude, сколько дольше 8 с.",
        "input_schema": {"type": "object", "properties": {"hours": {"type": "integer"}}},
    },
    {
        "name": "order_status",
        "description": (
            "Технический статус заказа по номеру: статусы заказа, оплаты и чека, перевозчик, номер отправления, "
            "отметки времени (оплата, напоминания, сборка, передача, пункт, вручение), попытки оплаты, "
            "сообщения клиенту по заказу (тип, отправлено ли, ошибка), уведомления менеджеру, события воронки, "
            "сбои сервисов по этому заказу. Получателя, контактов и адреса здесь нет."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "integer"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "client_status",
        "description": (
            "Техническое состояние диалога по peer_id (VK ID): когда писал, сколько сообщений, необработанные "
            "входящие, черновик (этап), заказы, вопросы менеджеру (только время, без текста), отписка, "
            "недоставляемость, тестовый ли аккаунт. Ни текстов, ни контактов здесь нет."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"peer_id": {"type": "integer"}},
            "required": ["peer_id"],
        },
    },
    {
        "name": "schedule_status",
        "description": (
            "Расписание и очереди: отметки задач (тик, пульс, отчёт Ops, выгрузки, копии) — когда отработали и "
            "сколько минут назад; очередь событий ВК (сколько ждёт, сколько ждёт самое старое, сколько брошено "
            "за сутки); недоставленные уведомления менеджеру; неотправленные сообщения клиентам за сутки по "
            "типам; необработанные входящие; доступна ли база; ревизия кода."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

from app.modules.ops.assistant import code as _code  # noqa: E402

TOOLS = TOOLS + _code.TOOLS
CODE_TOOLS = set(_code.HANDLERS)

_HANDLERS = {
    **_code.HANDLERS,
    "service_errors": service_errors,
    "event_queue": event_queue,
    "host_load": host_load,
    "api_latency": api_latency,
    "reply_times": reply_times,
    "order_status": order_status,
    "client_status": client_status,
    "schedule_status": schedule_status,
}


async def run(name: str, arguments: dict) -> dict:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"инструмента {name} нет"}
    try:
        return await handler(**{k: v for k, v in (arguments or {}).items() if v is not None})
    except TypeError:
        return {"error": "неверные параметры"}
    except RuntimeError:
        return {"error": "база недоступна" if name not in CODE_TOOLS else "репозиторий недоступен"}
    except Exception as error:
        # Сбой одного запроса — ответ модели «данных нет», а не провал всего
        # вопроса. Текст ошибки базы не отдаём: в нём бывают значения строк.
        logger.exception("Ассистент Ops: инструмент %s упал", name)
        return {"error": f"запрос не выполнился ({type(error).__name__})"}
