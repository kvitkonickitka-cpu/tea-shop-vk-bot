"""Тревоги Ops о выгрузке Т-Банка: что считается поломкой и что уходит в чат.

Главное: истёкший токен и упавшая задача не проходят молча, одна поломка —
одно сообщение (плюс напоминание раз в 12 ч) и одно 🟢, а текст ошибки
банка — в нём бывает номер счёта — в чат не попадает.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core import heartbeat
from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.ops import cashflow_watch as watch

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)  # 12:00 МСК
ACCOUNT = "40802810900010054673"
AUTH_ERROR = (
    "Банк отказал в доступе (HTTP 403). Проверьте токен, его скоуп «Счета и выписки» и что запрос "
    "идёт со статического IP ВМ. X-Request-Id=0c9f"
)


def _account(**overrides) -> dict:
    account = {
        "last4": "4673", "last_run_at": NOW - timedelta(hours=5), "last_success_at": NOW - timedelta(hours=5),
        "last_status": "ok", "last_error": None, "pending_checked_at": NOW - timedelta(hours=5),
        "pending_error": None, "synced_through": date(2026, 10, 17),
    }
    account.update(overrides)
    return account


def _state(accounts=None, beats=None) -> dict:
    marks = {"cashflow": NOW - timedelta(hours=5), "cashflow-backup": NOW - timedelta(hours=6)}
    marks.update(beats or {})
    return {"accounts": [_account()] if accounts is None else accounts, "beats": marks}


# --- вид ошибки --------------------------------------------------------------


def test_error_kind_from_cashflow_messages():
    assert watch.error_kind(AUTH_ERROR) == ("auth", 403)
    assert watch.error_kind("Банк отказал в доступе (HTTP 401). Проверьте токен") == ("auth", 401)
    assert watch.error_kind("Не удалось получить выписку за 5 попыток: HTTP 502, X-Request-Id=1") == ("http_5xx", 502)
    assert watch.error_kind("Не удалось получить выписку за 5 попыток: HTTP 429, X-Request-Id=1") == ("rate_limit", 429)
    # httpx.ReadTimeout печатается пустой строкой — остаётся только «за N попыток».
    assert watch.error_kind("Не удалось получить выписку за 5 попыток: ") == ("timeout", None)
    assert watch.error_kind("Не удалось получить выписку за 5 попыток: timed out") == ("timeout", None)
    assert watch.error_kind("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed") == ("tls", None)
    assert watch.error_kind("Банк ответил HTTP 400: {\"errorMessage\": \"bad\"}") == ("http_4xx", 400)
    assert watch.error_kind("Не разобрал дату операции: 'x'") == ("parse", None)
    assert watch.error_kind("division by zero") == ("other", None)


# --- что считается поломкой ----------------------------------------------------


def test_no_cashflow_here_is_silence():
    assert watch.evaluate({"accounts": [], "beats": {"расписание": NOW}}, NOW) == []


def test_healthy_cashflow_is_silence():
    assert watch.evaluate(_state(), NOW) == []


def test_auth_error_names_token_without_bank_text():
    state = _state([_account(last_status="error", last_error=f"{AUTH_ERROR} счёт {ACCOUNT}")])
    (problem,) = watch.evaluate(state, NOW)
    assert problem.key == "sync:4673"
    assert "<code>auth</code>, HTTP 403" in problem.text and "Токен T-API" in problem.text
    assert ACCOUNT not in problem.text and "X-Request-Id" not in problem.text
    assert "cashflow doctor" in problem.text


def test_no_success_for_too_long_is_a_problem():
    # Таймер не сработал: ошибки нет, но и выгрузки нет с прошлого утра.
    stale = NOW - watch.SYNC_STALE - timedelta(minutes=1)
    (problem,) = watch.evaluate(_state([_account(last_success_at=stale, last_run_at=stale)]), NOW)
    assert problem.key == "sync-stale:4673" and "07:00 и 21:00" in problem.text
    # Ночной промежуток 21:00 → 07:00 — ещё норма.
    assert watch.evaluate(_state([_account(last_success_at=NOW - timedelta(hours=14, minutes=5))]), NOW) == []


def test_unit_failure_counts_only_after_last_success():
    failed = watch.evaluate(_state(beats={"сбой:cashflow": NOW - timedelta(hours=1)}), NOW)
    assert [p.key for p in failed] == ["unit:cashflow"]
    assert "journalctl -u cashflow.service" in failed[0].text
    # Упала, потом прошла — уже не поломка.
    assert watch.evaluate(_state(beats={"сбой:cashflow": NOW - timedelta(hours=8)}), NOW) == []


def test_backup_failure_and_staleness():
    # Как 17.10.2026: скрипт копии не запустился (203/EXEC).
    failed = watch.evaluate(_state(beats={"сбой:cashflow-backup": NOW - timedelta(hours=1)}), NOW)
    assert [p.key for p in failed] == ["unit:cashflow-backup"]
    assert "Резервная копия схемы finance упала" in failed[0].text
    stale = watch.evaluate(_state(beats={"cashflow-backup": NOW - timedelta(hours=27)}), NOW)
    assert [p.key for p in stale] == ["backup-stale"]


def test_holds_error_is_a_warning():
    state = _state([_account(pending_error="Не удалось получить выписку за 5 попыток: HTTP 503, X-Request-Id=1")])
    (problem,) = watch.evaluate(state, NOW)
    assert problem.key == "holds:4673" and problem.text.startswith("⚠️")


# --- что уходит в чат ----------------------------------------------------------


@pytest.fixture
def ops_chat(monkeypatch):
    from app.modules.dialog import telegram_client

    sent: list[str] = []

    async def send_message(text_, chat_id=None):
        assert chat_id == "-100500"
        sent.append(text_)

    monkeypatch.setattr(telegram_client, "send_message", send_message)
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100500")
    return sent


@pytest.fixture
def cashflow_state(monkeypatch):
    holder = {"state": _state()}

    async def read_state():
        # Отметки «уже объявлено» — настоящие, из heartbeats: их пишет check.
        beats = dict(holder["state"]["beats"])
        async with get_session_factory()() as session:
            rows = await session.execute(text("select name, last_run_at from heartbeats"))
            beats.update({name: moment for name, moment in rows})
        return {"accounts": holder["state"]["accounts"], "beats": beats}

    monkeypatch.setattr(watch, "read_state", read_state)
    return holder


async def test_one_message_reminder_and_recovery(clean, ops_chat, cashflow_state):
    cashflow_state["state"] = _state([_account(last_status="error", last_error=AUTH_ERROR)])
    assert (await watch.check(NOW))["sent"] == ["sync:4673"]
    assert (await watch.check(NOW + timedelta(minutes=5)))["sent"] == []
    assert len(ops_chat) == 1 and ops_chat[0].startswith("🔴 <b>Выгрузка Т-Банка не прошла</b>")

    # Вид сменился — поломка та же: ни 🟢, ни второго сообщения.
    cashflow_state["state"] = _state([_account(last_status="error", last_error="Не удалось … за 5 попыток: ")])
    result = await watch.check(NOW + timedelta(minutes=10))
    assert result["sent"] == [] and result["resolved"] == [] and len(ops_chat) == 1

    # Через 12 часов — напоминание.
    assert (await watch.check(NOW + watch.REPEAT))["sent"] == ["sync:4673"]
    assert len(ops_chat) == 2

    # Починилось — одно 🟢, и отметка «объявлено» убрана.
    fixed = NOW + watch.REPEAT
    cashflow_state["state"] = _state([_account(last_run_at=fixed, last_success_at=fixed)])
    result = await watch.check(NOW + watch.REPEAT + timedelta(minutes=5))
    assert result["resolved"] == ["sync:4673"]
    assert ops_chat[-1] == "🟢 <b>Cashflow снова в порядке</b>: выгрузка по счёту …4673."
    assert await heartbeat.last_run(watch.NOTICE_PREFIX + "sync:4673") is None
    assert (await watch.check(NOW + watch.REPEAT + timedelta(minutes=10)))["resolved"] == []
    assert len(ops_chat) == 3


async def test_quiet_when_all_is_well(clean, ops_chat, cashflow_state):
    for minutes in range(0, 30, 5):
        await watch.check(NOW + timedelta(minutes=minutes))
    assert ops_chat == []


async def test_no_ops_chat_no_work(clean, monkeypatch):
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "")
    assert await watch.check(NOW) == {"skipped": "нет чата Ops"}


async def test_read_state_without_finance_schema(clean, db):
    async with db() as session:
        exists = (await session.execute(text("select to_regclass('finance.sync_state')"))).scalar()
    if exists:
        pytest.skip("в тестовой базе есть схема finance — проверка «схемы нет» не про неё")
    await heartbeat.note("cashflow")
    state = await watch.read_state()
    assert state["accounts"] == [] and "cashflow" in state["beats"]


async def test_read_state_reads_sync_state(clean, db):
    async with db() as session:
        if (await session.execute(text("select to_regnamespace('finance')"))).scalar():
            pytest.skip("схема finance уже есть — не трогаем чужие данные")
        await session.execute(text("create schema finance"))
        await session.execute(text(
            "create table finance.sync_state (account_number text primary key, "
            "synced_through date, last_run_at timestamptz, last_success_at timestamptz, last_status text, "
            "last_error text, operations_seen bigint not null default 0, pending_checked_at timestamptz, "
            "pending_error text)"
        ))
        await session.execute(text(
            "insert into finance.sync_state (account_number, last_run_at, last_success_at, last_status) "
            "values (:account, now(), now(), 'ok')"
        ), {"account": ACCOUNT})
        await session.commit()
    try:
        state = await watch.read_state()
        (account,) = state["accounts"]
        assert account["last4"] == "4673" and account["last_success_at"].tzinfo is not None
    finally:
        async with db() as session:
            await session.execute(text("drop schema finance cascade"))
            await session.commit()


# --- ассистент Ops ---------------------------------------------------------------


async def test_assistant_tool_hides_bank_text(monkeypatch):
    from app.modules.ops.assistant import tools

    async def read_state():
        return _state([_account(last_status="error", last_error=f"{AUTH_ERROR} {ACCOUNT}")],
                      beats={"сбой:cashflow": NOW})

    async def rows(sql, **params):
        assert "raw_operations" in sql
        return [{"latest_operation_date": date(2026, 10, 16), "unclassified_operations": 0}]

    monkeypatch.setattr(watch, "read_state", read_state)
    monkeypatch.setattr(tools, "_rows", rows)
    data = await tools.run("cashflow_status", {})
    dumped = json.dumps(data, ensure_ascii=False)
    assert data["accounts"][0]["error_kind"] == "auth" and data["accounts"][0]["error_http_code"] == 403
    assert data["data"]["latest_operation_date"] == "2026-10-16"
    assert "сбой:cashflow" in data["systemd_marks"] and "sync:4673" in data["problems_now"]
    assert ACCOUNT not in dumped and "Проверьте токен" not in dumped
    assert any(tool["name"] == "cashflow_status" for tool in tools.TOOLS)


async def test_failure_marks_are_not_tasks_in_report(clean):
    from app.modules.ops import report

    await heartbeat.note("cashflow")
    await heartbeat.note("сбой:cashflow")
    assert set(await report._task_times()) == {"cashflow"}
