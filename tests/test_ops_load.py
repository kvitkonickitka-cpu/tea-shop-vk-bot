"""Замеры загрузки ВМ и время ответа сервисов — для ассистента Ops."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import get_session_factory
from app.modules.ops import journal, resources
from app.modules.ops.assistant import tools


def test_minute_summary_keeps_the_peak():
    resources._samples[:] = [
        {"cpu": 10.0, "steal": 1.0, "mem_available_mb": 2000, "mem_total_mb": 3911, "bot_mem_mb": 120, "load1": 0.1},
        {"cpu": 85.0, "steal": 3.0, "mem_available_mb": 1800, "mem_total_mb": 3911, "bot_mem_mb": 140, "load1": 0.9},
    ]
    summary = resources.minute_summary()
    assert summary["cpu_max"] == 85.0 and summary["cpu_avg"] == 47.5
    assert summary["mem_available_mb"] == 1800 and summary["bot_mem_mb"] == 140
    assert resources.minute_summary() == {}  # забрали — начали заново


async def test_watch_records_successful_calls(monkeypatch):
    journal._BUFFER.clear()

    @journal.watch("cdek", "расчёт тарифов")
    async def quote():
        return 42

    assert await quote() == 42
    calls = [row for row in journal._BUFFER if row["kind"] == "call"]
    assert calls and calls[0]["api"] == "cdek" and calls[0]["operation"] == "расчёт тарифов"
    journal._BUFFER.clear()
    # Выгрузка каталога пунктов Ozon в журнал не пишется — её сотни за проход.
    journal.note_call("ozon", "/v1/delivery-point/list", 0.05)
    journal.note_call("ozon", "расчёт (checkout)", 0.3)
    assert [row["operation"] for row in journal._BUFFER] == ["расчёт (checkout)"]
    journal._BUFFER.clear()


async def test_latency_and_load_tools(db):
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        await session.execute(text("delete from ops_events"))
        for ms in (300, 400, 2500):
            await session.execute(text(
                "insert into ops_events (at, kind, api, operation, duration_ms) values (:at, 'call', 'cdek', 'расчёт тарифов', :ms)"
            ), {"at": now, "ms": ms})
        minute = now.replace(second=10, microsecond=0)
        await session.execute(text(
            "insert into ops_events (at, kind, data) values (:at, 'pulse', cast(:d as jsonb))"
        ), {"at": minute, "d": '{"cpu_max": 70.0, "cpu_avg": 20.0, "mem_available_mb": 1900, "bot_mem_mb": 150}'})
        await session.execute(text(
            "insert into ops_events (at, kind, data) values (:at, 'pulse', cast(:d as jsonb))"
        ), {"at": minute - timedelta(minutes=5), "d": '{"cpu_max": 5.0, "cpu_avg": 2.0, "mem_available_mb": 2100}'})
        await session.execute(text(
            "insert into ops_events (at, kind, duration_ms, llm_ms) values (:at, 'turn', 6500, 5900)"
        ), {"at": minute + timedelta(seconds=20)})
        await session.commit()

    latency = await tools.api_latency(1)
    row = latency["calls"][0]
    assert row["api"] == "cdek" and row["calls"] == 3 and row["p50_ms"] == 400 and row["max_ms"] == 2500

    load = await tools.host_load(1)
    assert load["minutes_with_client_turns"]["count"] == 1
    assert load["minutes_with_client_turns"]["cpu_max"] == 70.0
    assert load["minutes_without_turns"]["cpu_max"] == 5.0
    assert load["busiest_minutes"][0]["turns"] == 1 and load["busiest_minutes"][0]["slowest_turn_s"] == 6.5


async def test_event_queue_tool_shows_counts_but_no_message_text(db):
    import json

    async with get_session_factory()() as session:
        await session.execute(text("delete from queued_events"))
        await session.execute(text(
            "insert into queued_events (body, done_at, attempts) values (cast(:b as jsonb), now(), 1)"
        ), {"b": json.dumps({"type": "message_new", "event_id": "e1",
                             "object": {"message": {"peer_id": 111, "text": "Иванов Иван +79001234567"}}})})
        await session.execute(text(
            "insert into queued_events (body, attempts, last_error) values (cast(:b as jsonb), 2, 'RuntimeError')"
        ), {"b": json.dumps({"type": "message_new", "object": {"message": {"peer_id": 222, "text": "секрет"}}})})
        await session.commit()

    data = await tools.event_queue(24)
    total = sum(row["events"] for row in data["by_hour_and_type"])
    assert total == 2
    row = data["by_hour_and_type"][0]
    assert row["type"] == "message_new" and row["done"] == 1 and row["waiting"] == 1 and row["with_error"] == 1
    assert {r["peer_id"] for r in data["recent"]} == {"111", "222"}
    dumped = json.dumps(data, ensure_ascii=False, default=str)
    assert "Иванов" not in dumped and "79001234567" not in dumped and "секрет" not in dumped
