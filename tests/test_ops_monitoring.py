"""Мониторинг: обёртки на внешние сервисы, пульс, отчёт, маскировка логов.

Главное, что проверяется здесь, — не что метрики красивые, а что
мониторинг не меняет поведение бота: исключение из обёртки выходит то же
самое, замер после отправки не роняет ход, сбой базы и Monitoring не
бросает наружу.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.core import logs
from app.core.config import settings
from app.modules.ops import alerts, host, journal, monitoring, pulse, report


@pytest.fixture(autouse=True)
def empty_buffer():
    journal._BUFFER.clear()
    yield
    journal._BUFFER.clear()


@pytest.fixture
def sent_metrics(monkeypatch):
    sent: list[list[dict]] = []

    async def write(metrics):
        sent.append(metrics)
        return True

    monkeypatch.setattr(monitoring, "write", write)
    return sent


def _metric(metrics, name, **labels):
    for metric in metrics:
        if metric["name"] == name and metric.get("labels", {}) == labels:
            return metric["value"]
    return None


# --- классификация ---------------------------------------------------------


def test_classify():
    from app.modules.delivery.cdek_client import CdekError
    from app.modules.payment.yookassa_client import YooKassaUnknown

    assert journal.classify(httpx.ReadTimeout("slow")) == ("timeout", None)
    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as cause:
            raise YooKassaUnknown("ЮKassa не ответила") from cause
    except YooKassaUnknown as error:
        assert journal.classify(error) == ("network", None)
    assert journal.classify(CdekError("СДЭК не выдал токен — HTTP 401; x")) == ("auth", 401)
    assert journal.classify(CdekError("Расчёт СДЭК не удался — HTTP 502")) == ("http_5xx", 502)
    assert journal.classify(CdekError("Расчёт — HTTP 400; v2_bad_request")) == ("http_4xx", 400)
    assert journal.classify(CdekError("СДЭК не знает города «Мсква»")) == ("validation", None)

    class Overloaded(Exception):
        status_code = 529

    assert journal.classify(Overloaded()) == ("http_5xx", 529)
    assert journal.classify(ValueError("no text block")) == ("other", None)


# --- обёртка -----------------------------------------------------------------


async def test_watch_passes_the_same_exception_and_records_once():
    original = RuntimeError("Ozon отказал на /x — HTTP 503")

    @journal.watch("ozon", "внутренний")
    async def inner():
        raise original

    @journal.watch("ozon", "внешний")
    async def outer():
        await inner()

    with pytest.raises(RuntimeError) as caught:
        await outer()
    assert caught.value is original
    rows = list(journal._BUFFER)
    assert len(rows) == 1
    assert rows[0]["api"] == "ozon" and rows[0]["operation"] == "внутренний"
    assert rows[0]["error_kind"] == "http_5xx" and rows[0]["http_status"] == 503


async def test_watch_keeps_results_and_counts_claude_time():
    @journal.watch("claude", "ход")
    async def converse():
        await asyncio.sleep(0.05)
        return "ответ"

    holder = journal.start_turn()
    assert await converse() == "ответ"
    assert holder[0] >= 0.04
    assert not journal._BUFFER


async def test_cancellation_is_not_an_error():
    @journal.watch("cdek", "расчёт")
    async def slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not journal._BUFFER


async def test_order_scope_puts_order_into_the_error():
    @journal.watch("cdek", "создание заказа")
    async def register_order():
        raise RuntimeError("СДЭК не принял заказ — HTTP 500")

    @journal.order_scope
    async def register(*, order_id=None):
        await register_order()

    with pytest.raises(RuntimeError):
        await register(order_id=1042)
    assert journal._BUFFER[0]["order_id"] == 1042


async def test_yookassa_operation_hides_ids():
    from app.modules.payment import yookassa_client

    assert yookassa_client._operation("GET", "/payments/2f7a0c1e-000f-5000-9000-1b2c3d4e5f60") == "GET /payments/{id}"
    assert yookassa_client._operation("POST", "/payments") == "POST /payments"


async def test_finish_turn_never_raises(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("мониторинг сломался")

    monkeypatch.setattr(journal, "note_turn", broken)
    journal.finish_turn(0.0, [0.0])  # не бросает


async def test_waited_counts_from_the_client_message():
    import time

    journal.mark_received(time.time() - 7)
    assert 6.5 < journal.waited(time.monotonic()) < 8
    journal.mark_received("мусор")
    assert journal.waited(time.monotonic()) < 1


# --- буфер и база ------------------------------------------------------------


async def test_flush_writes_and_survives_a_dead_database(clean, monkeypatch):
    journal.note_turn(3.0, 2.0)
    assert await journal.flush() == 1
    async with clean() as session:
        assert (await session.execute(text("select count(*) from ops_events where kind='turn'"))).scalar_one() == 1

    async def dead(*args):
        raise OSError("база не отвечает")

    monkeypatch.setattr(journal, "_insert", dead)
    journal.note_turn(4.0, 1.0)
    assert await journal.flush() == 0
    assert journal.pending() == 1  # запись ждёт следующего раза


# --- пульс -------------------------------------------------------------------


async def test_pulse_metrics(clean, sent_metrics):
    for seconds in (5, 6, 7, 40):
        journal.note_turn(seconds, seconds / 2)
    error = RuntimeError("HTTP 503")
    for _ in range(3):
        journal.note_error("cdek", "расчёт тарифов", error, 1.0)
    journal.note_error("cdek", "пункты", RuntimeError("СДЭК не знает города «Мсква»"), 0.1)

    result = await pulse.run()
    metrics = sent_metrics[0]
    assert result["db_up"] is True
    assert _metric(metrics, "bot_heartbeat") == 1
    assert _metric(metrics, "bot_db_up") == 1
    assert _metric(metrics, "bot_turns") == 4
    assert _metric(metrics, "external_api_errors", api="cdek") == 3  # опечатка не в счёт
    assert _metric(metrics, "external_api_validation", api="cdek") == 1
    assert _metric(metrics, "external_api_errors", api="yookassa") == 0
    assert 30 < _metric(metrics, "bot_response_p95_seconds", stage="total") <= 40
    assert _metric(metrics, "bot_response_p95_seconds", stage="llm") <= 20
    async with clean() as session:
        pulses = (await session.execute(text("select count(*) from ops_events where kind='pulse'"))).scalar_one()
    assert pulses == 1


async def test_pulse_without_turns_sends_no_latency(clean, sent_metrics):
    await pulse.run()
    names = {metric["name"] for metric in sent_metrics[0]}
    assert "bot_response_p95_seconds" not in names
    assert _metric(sent_metrics[0], "bot_turns") == 0


async def test_pulse_with_dead_database_still_beats(monkeypatch, sent_metrics):
    def no_db():
        raise RuntimeError("DATABASE_URL is not configured")

    monkeypatch.setattr(pulse, "get_session_factory", no_db)
    result = await pulse.run()
    assert result["db_up"] is False
    assert _metric(sent_metrics[0], "bot_heartbeat") == 1
    assert _metric(sent_metrics[0], "bot_db_up") == 0


async def test_monitoring_write_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "yc_folder_id", "b1gtest")

    class Broken(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=httpx.MockTransport(self._fail), **kwargs)

        @staticmethod
        def _fail(request):
            raise httpx.ConnectError("нет сети")

    monkeypatch.setattr(monitoring.httpx, "AsyncClient", Broken)
    monkeypatch.setattr(monitoring, "_token", "")
    assert await monitoring.write([monitoring.gauge("bot_heartbeat", 1)]) is False


async def test_monitoring_write_request(monkeypatch):
    monkeypatch.setattr(settings, "yc_folder_id", "b1gtest")
    seen = []

    def handler(request):
        seen.append(request)
        if "169.254.169.254" in str(request.url):
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "iam-t", "expires_in": 3600})
        return httpx.Response(200, json={})

    original = httpx.AsyncClient

    class Mocked(original):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(monitoring.httpx, "AsyncClient", Mocked)
    monkeypatch.setattr(monitoring, "_token", "")
    assert await monitoring.write([monitoring.gauge("external_api_errors", 2, api="cdek")]) is True
    write = seen[-1]
    assert write.url.params["folderId"] == "b1gtest" and write.url.params["service"] == "custom"
    assert write.headers["Authorization"] == "Bearer iam-t"
    body = json.loads(write.content)
    assert body["metrics"][0]["labels"] == {"api": "cdek"} and body["metrics"][0]["ts"].endswith("Z")


# --- триггер -----------------------------------------------------------------


async def test_pulse_timer_runs_the_pulse_not_the_tick(clean, monkeypatch, sent_metrics):
    from app.api import internal
    from app.main import app

    monkeypatch.setattr(settings, "internal_api_token", "t0ken")

    async def tick():
        raise AssertionError("пульс не должен запускать тик расписания")

    monkeypatch.setattr(internal, "_run_scheduled", tick)
    body = {"messages": [{
        "event_metadata": {"event_type": "yandex.cloud.events.serverless.triggers.TimerMessage"},
        "details": {"trigger_id": "a1s", "payload": "ops-pulse t0ken"},
    }]}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/", content=json.dumps(body))
    assert response.json()["db_up"] is True
    assert _metric(sent_metrics[0], "bot_heartbeat") == 1


async def test_emulation_reaches_the_pulse_but_not_the_report(clean, monkeypatch, sent_metrics):
    from app.main import app

    monkeypatch.setattr(settings, "internal_api_token", "t0ken")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/internal/ops/emulate?api=ozon&count=3", content=b"t0ken")
    assert response.json()["pulse"]["errors"]["ozon"] == 3
    assert _metric(sent_metrics[-1], "external_api_errors", api="ozon") == 3

    data = await report.collect()
    assert data["stats"]["errors"] == {}


# --- отчёт -------------------------------------------------------------------


async def test_daily_report_once_a_day(clean, monkeypatch):
    from app.core import heartbeat
    from app.modules.dialog import telegram_client

    sent = []

    async def send_message(text_, chat_id=None):
        sent.append((chat_id, text_))

    monkeypatch.setattr(telegram_client, "send_message", send_message)
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100500")

    journal.note_turn(5.0, 3.0)
    journal.note_error("claude", "ход диалога", TimeoutError(), 30.0)
    await heartbeat.note("расписание")
    await pulse.run()

    morning = datetime(2026, 10, 3, 5, 30, tzinfo=timezone.utc)  # 08:30 МСК
    assert (await report.send(now=morning))["skipped"] == "рано"
    after_nine = morning + timedelta(hours=1)
    first = await report.send(now=after_nine)
    assert first["sent"] is True
    # Отметку ставят настоящие часы, а тест живёт в вымышленном утре.
    async with clean() as session:
        await session.execute(
            text("update heartbeats set last_run_at = :at where name = :name"),
            {"at": after_nine, "name": report.HEARTBEAT},
        )
        await session.commit()
    assert (await report.send(now=after_nine + timedelta(minutes=5)))["skipped"] == "сегодня уже отправлен"

    # Пульс прислал подробность сбоя Claude, отчёт — отдельным сообщением.
    assert any(text_.startswith("🔎 <b>Claude</b>") for _, text_ in sent)
    chat, text_ = next((c, t) for c, t in sent if t.startswith("📊"))
    assert chat == "-100500"
    assert "Аптайм" in text_ and "Claude — 1 (timeout 1) ⚠️" in text_
    assert "ответов 1" in text_ and "Диалогов" in text_
    assert "тик бота" in text_


async def test_report_without_ops_chat_is_silent(monkeypatch):
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "")
    assert "skipped" in await report.send(force=True)


def test_render_marks_stale_tasks_and_full_disk():
    now = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)
    data = {
        "now": now,
        "stats": {"errors": {}, "turns": 0, "p95": None, "p50": None, "llm_p95": None, "llm_p50": None},
        "pulses": 1300, "expected": 1440, "dialogs": 0,
        "disk": host.Disk(percent=91, total_bytes=9 * 1024 ** 3, free_bytes=800 * 1024 ** 2, db_bytes=49 * 1024 ** 2),
        "tasks": {"cashflow": now - timedelta(hours=30), "расписание": now - timedelta(minutes=4)},
    }
    rendered = report.render(data)
    assert "выписка Т-Банка — 02.10 03:00 ⚠️ давно не было" in rendered
    assert "тик бота (каждые 5 мин) — 03.10 08:56 ✅" in rendered
    assert "91 % 🔴 — свободно 0,8 ГБ из 9,0 ГБ · база 49 МБ" in rendered
    assert "90,3 %" in rendered and "⚠️" in rendered.split("\n")[2]


# --- логи --------------------------------------------------------------------


def test_mask_removes_personal_data_and_secrets():
    raw = (
        "клиент +7 (921) 447-76-22 и 89214477622, почта ivan.petrov@mail.ru, "
        "бот 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1, "
        "postgresql://teashop:s3cr3t@10.129.0.4:5432/teashop, client_secret=abc123&x=1, "
        "Authorization: Bearer t1.9euelZqXjZ, заказ 1042, peer_id=111449231"
    )
    masked = logs.mask(raw)
    for leaked in ("447-76-22", "89214477622", "ivan.petrov", "AAHdq", "s3cr3t", "abc123", "t1.9eu"):
        assert leaked not in masked
    assert "заказ 1042" in masked and "peer_id=111449231" in masked


def test_json_formatter_fields_and_levels():
    formatter = logs.JsonFormatter()
    record = logging.LogRecord("app.x", logging.WARNING, __file__, 1, "Ошибка %s: тел. %s", ("СДЭК", "89001234567"), None)
    record.service, record.order_id, record.http_status = "cdek", 1042, 502
    data = json.loads(formatter.format(record))
    assert data["level"] == "WARN"
    assert data["message"] == "Ошибка СДЭК: тел. <телефон>"
    assert data["service"] == "cdek" and data["order_id"] == 1042 and data["http_status"] == 502


# --- замер ответа в настоящем ходе -------------------------------------------


async def test_respond_records_only_delivered_replies(monkeypatch):
    import time

    from app.modules.dialog import attachments, service, vk_client
    from app.modules.orders import conversation

    @journal.watch("claude", "ход диалога")
    async def fake_claude():
        await asyncio.sleep(0.02)
        return "Добрый день!"

    async def handle_turn(peer_id, text_, attached, budget_seconds=None):
        return await fake_claude()

    sent = []

    async def send_message(peer_id, reply, **kwargs):
        sent.append(reply)

    async def typing(peer_id):
        return None

    monkeypatch.setattr(conversation, "handle_turn", handle_turn)
    monkeypatch.setattr(vk_client, "send_message", send_message)
    monkeypatch.setattr(service, "_set_typing_quietly", typing)

    journal.mark_received(time.time() - 5)
    await service.respond(1, "привет", attachments.Collected())
    assert sent == ["Добрый день!"]
    turn = journal._BUFFER[-1]
    assert turn["kind"] == "turn" and turn["duration_ms"] >= 4900 and turn["llm_ms"] >= 15

    async def vk_down(peer_id, reply, **kwargs):
        raise RuntimeError("VK API error")

    journal._BUFFER.clear()
    monkeypatch.setattr(vk_client, "send_message", vk_down)
    with pytest.raises(RuntimeError):
        await service.respond(1, "привет", attachments.Collected())
    assert not [row for row in journal._BUFFER if row["kind"] == "turn"]


# --- подробности сбоев --------------------------------------------------------


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


async def test_details_say_what_broke(clean, ops_chat, sent_metrics):
    from app.modules.ops import details

    @journal.watch("cdek", "создание заказа")
    async def register_order():
        raise RuntimeError("СДЭК не выдал токен — HTTP 401; v2_token_expired")

    @journal.order_scope
    async def register(*, order_id):
        await register_order()

    for order_id in (1042, 1045):
        with pytest.raises(RuntimeError):
            await register(order_id=order_id)
    journal.note_error("cdek", "пункты", RuntimeError("СДЭК не знает города «Мсква»"), 0.1)

    await pulse.run()
    assert len(ops_chat) == 1
    message = ops_chat[0]
    assert message.startswith("🔎 <b>СДЭК</b> — 2 сбоя")
    assert "создание заказа — HTTP 401, отказ в доступе — похоже, истёк или сменился ключ · заказ #1042" in message
    assert "Заказы: #1042, #1045" in message
    assert "Мсква" not in message and "пункты" not in message  # опечатка клиента — не сбой

    # Новый сбой в пределах паузы ждёт, а не шлётся сразу.
    journal.note_error("cdek", "статус заказа", httpx.ReadTimeout("slow"), 15.0)
    await pulse.run()
    assert len(ops_chat) == 1

    # Пауза прошла — уходит только новый.
    later = datetime.now(timezone.utc) + details.COOLDOWN + timedelta(seconds=1)
    result = await details.send_pending(now=later)
    assert result["sent"] == {"cdek": 1}
    assert "статус заказа — таймаут" in ops_chat[1] and "#1042" not in ops_chat[1]


async def test_details_are_released_when_telegram_fails(clean, monkeypatch, sent_metrics):
    from app.modules.dialog import telegram_client
    from app.modules.ops import details

    async def down(text_, chat_id=None):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(telegram_client, "send_message", down)
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100500")
    journal.note_error("ozon", "/v1/delivery/checkout", RuntimeError("HTTP 503"), 1.0)

    result = await pulse.run()  # не бросает
    assert "details_sent" not in result
    async with clean() as session:
        waiting = (await session.execute(text(
            "select count(*) from ops_events where kind='error' and notified_at is null"
        ))).scalar_one()
    assert waiting == 1  # следующий пульс попробует снова
    assert (await details.send_pending())["sent"] == {}  # Telegram всё ещё лежит


def test_details_wording():
    from app.modules.ops import details

    at = datetime(2026, 10, 3, 21, 24, tzinfo=timezone.utc)
    row = {"at": at, "operation": journal.EMULATION, "error_kind": "http_5xx", "http_status": 503, "order_id": None}
    rendered = details.render("yookassa", [row] * 7)
    assert rendered.startswith("🔎 <b>ЮKassa</b> — 7 сбоев (эмуляция)")
    assert "00:24 · эмуляция — HTTP 503, сбой на стороне сервиса" in rendered
    assert "и ещё 2 раньше" in rendered
    assert details._failures(21) == "21 сбой" and details._failures(12) == "12 сбоев"


# --- сообщения бота о реальных проблемах -------------------------------------


@pytest.fixture
def no_db_state():
    alerts.reset()
    yield
    alerts.reset()


def test_parse_df():
    lines = [
        "Filesystem     1024-blocks    Used Available Capacity Mounted on",
        "/dev/vda1          9485204 5000000   4100000      56% /var/lib/postgresql/data",
    ]
    assert host.parse_df(lines) == (56, 9485204 * 1024, 4100000 * 1024)
    assert host.parse_df(["мусор"]) is None


async def test_disk_through_the_database(clean):
    disk = await host.disk()
    assert disk.db_bytes and disk.db_bytes > 0
    assert disk.percent is not None and 0 <= disk.percent <= 100  # тестовая база — суперпользователь


async def test_slow_replies_need_several_turns(clean, ops_chat):
    stats = {"turns": 2, "p95": 90.0, "llm_p95": 80.0}
    assert await alerts.check_slow(stats) is False  # два ответа — ещё не тенденция
    stats["turns"] = 5
    assert await alerts.check_slow(stats) is True
    assert "p95 90 с" in ops_chat[0] and "тормозит модель" in ops_chat[0]
    assert await alerts.check_slow(stats) is False  # пауза час
    assert await alerts.check_slow({"turns": 5, "p95": 40.0, "llm_p95": 10.0},
                                   now=datetime.now(timezone.utc) + timedelta(hours=2)) is False


async def test_database_down_once_and_back(ops_chat, no_db_state, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(alerts.time, "time", lambda: clock[0])
    assert await alerts.check_db(False) is None  # один пропуск — не повод
    clock[0] += 120
    assert await alerts.check_db(False) == "down"
    clock[0] += 60
    assert await alerts.check_db(False) is None  # не каждую минуту
    clock[0] += 60
    assert await alerts.check_db(True) == "up"
    assert await alerts.check_db(True) is None
    assert len(ops_chat) == 2 and "не отвечает" in ops_chat[0] and "снова отвечает" in ops_chat[1]


async def test_disk_alarm_once(clean, ops_chat, monkeypatch):
    async def full():
        return host.Disk(percent=91, total_bytes=10 * 1024 ** 3, free_bytes=900 * 1024 ** 2, db_bytes=150 * 1024 ** 2)

    monkeypatch.setattr(host, "disk", full)
    assert (await alerts.check_disk())["sent"] == "alarm"
    assert "sent" not in await alerts.check_disk()
    assert ops_chat[0].startswith("🔴 <b>Диск ВМ с базой заполнен на 91 %</b>")

    async def fine():
        return host.Disk(percent=56, total_bytes=10 * 1024 ** 3, free_bytes=4 * 1024 ** 3, db_bytes=1)

    monkeypatch.setattr(host, "disk", fine)
    assert "sent" not in await alerts.check_disk()
    assert len(ops_chat) == 1


async def test_quiet_pulse_sends_nothing(clean, ops_chat, sent_metrics, no_db_state):
    # Клиентов нет, сбоев нет — в Ops ни одного сообщения, сколько бы пульсов ни было.
    for _ in range(5):
        await pulse.run()
    assert ops_chat == []
