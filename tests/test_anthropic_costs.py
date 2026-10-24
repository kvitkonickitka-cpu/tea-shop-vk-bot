"""Счёт Anthropic (cost_report): разбор, повторы, замена дней, без утечки ключа.

Anthropic подменён: настоящих запросов нет, ключ — ненастоящий.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.modules.ops import anthropic_costs as costs
from app.modules.ops.models import AnthropicCost

KEY = "sk-ant-admin01-test-key-not-real-0123456789"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def bucket(day: str, *results):
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return {"starting_at": start.isoformat().replace("+00:00", "Z"),
            "ending_at": (start + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "results": list(results)}


def item(amount, token_type="output_tokens", model="claude-sonnet-5-5"):
    return {"amount": amount, "currency": "USD", "cost_type": "tokens", "model": model,
            "token_type": token_type, "service_tier": "standard",
            "description": f"Claude Sonnet 5.5 Usage - {token_type}"}


class Anthropic:
    def __init__(self, *pages_or_errors):
        self.answers = list(pages_or_errors)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        return httpx.Response(200, json=answer)

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_admin_key", KEY)
    monkeypatch.setattr(settings, "anthropic_base_url", "https://94-228-170-170.nip.io")
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100")


@pytest.fixture
def ops(monkeypatch):
    sent = []

    async def notify(kind, text_, **kwargs):
        sent.append((kind, text_, kwargs.get("chat_id")))
        return True

    monkeypatch.setattr("app.messages.manager.notify", notify)
    return sent


async def test_fetch_reads_pages_in_dollars_through_the_proxy(key):
    api = Anthropic(
        {"data": [bucket("2026-10-16", item("123.45"), item("10", "uncached_input_tokens"))],
         "has_more": True, "next_page": "page_2"},
        {"data": [bucket("2026-10-17"), bucket("2026-10-18", item("0.5", "cache_read_input_tokens"))],
         "has_more": False, "next_page": None},
    )
    rows = await costs.fetch(date(2026, 10, 16), date(2026, 10, 19), transport=api.transport, sleep=api.sleep)
    assert [(r["day"], r["token_type"], r["amount_usd"]) for r in rows] == [
        (date(2026, 10, 16), "output_tokens", Decimal("1.234500")),  # центы → доллары
        (date(2026, 10, 16), "uncached_input_tokens", Decimal("0.100000")),
        (date(2026, 10, 18), "cache_read_input_tokens", Decimal("0.005000")),
    ]
    first, second = api.requests
    assert first.url.host == "94-228-170-170.nip.io" and first.url.path == "/v1/organizations/cost_report"
    assert first.headers["x-api-key"] == KEY and first.headers["anthropic-version"] == "2023-06-01"
    query = parse_qs(urlsplit(str(first.url)).query)
    assert query["group_by[]"] == ["description"] and query["bucket_width"] == ["1d"]
    assert query["starting_at"] == ["2026-10-16T00:00:00Z"] and query["ending_at"] == ["2026-10-19T00:00:00Z"]
    assert parse_qs(urlsplit(str(second.url)).query)["page"] == ["page_2"]


async def test_bad_key_is_explained_without_leaking_it(key, caplog):
    api = Anthropic(401)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(costs.CostReportError) as error:
            await costs.fetch(date(2026, 10, 1), date(2026, 10, 2), transport=api.transport, sleep=api.sleep)
    assert "не принял админ-ключ" in str(error.value) and not error.value.transient
    assert KEY not in str(error.value) and KEY[-12:] not in caplog.text and api.sleeps == []


async def test_temporary_trouble_is_retried(key):
    api = Anthropic(503, httpx.ConnectTimeout("t"), {"data": [], "has_more": False})
    assert await costs.fetch(date(2026, 10, 1), date(2026, 10, 2), transport=api.transport, sleep=api.sleep) == []
    assert api.sleeps == [2.0, 5.0]

    api = Anthropic(503, 503, 503)
    with pytest.raises(costs.CostReportError) as error:
        await costs.fetch(date(2026, 10, 1), date(2026, 10, 2), transport=api.transport, sleep=api.sleep)
    assert error.value.transient


async def test_without_key_nothing_happens(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_admin_key", "")
    assert "skipped" in await costs.sync()


async def rows_in(db):
    async with db() as session:
        return [(r.day, r.amount_usd) for r in (await session.execute(
            select(AnthropicCost).order_by(AnthropicCost.day, AnthropicCost.id))).scalars()]


async def test_first_sync_backfills_then_refreshes_recent_days(clean, key, ops):
    first = Anthropic({"data": [bucket("2026-08-01", item("100")), bucket("2026-10-17", item("200"))],
                       "has_more": False})
    result = await costs.sync(now=NOW, transport=first.transport, sleep=first.sleep)
    assert result["строк"] == 2 and result["итого, $"] == "3.000000"
    starting = parse_qs(urlsplit(str(first.requests[0].url)).query)["starting_at"][0]
    assert starting == "2026-07-21T00:00:00Z"  # первый раз — 90 дней

    # Через час — последние 7 дней: 17.10 уточнили, 01.08 остаётся как было.
    later = NOW + timedelta(hours=1, minutes=1)
    second = Anthropic({"data": [bucket("2026-10-17", item("250"))], "has_more": False})
    await costs.sync(now=later, transport=second.transport, sleep=second.sleep)
    starting = parse_qs(urlsplit(str(second.requests[0].url)).query)["starting_at"][0]
    assert starting == "2026-10-12T00:00:00Z"
    assert await rows_in(clean) == [(date(2026, 8, 1), Decimal("1.000000")), (date(2026, 10, 17), Decimal("2.500000"))]

    # Меньше часа — не читает.
    assert "skipped" in await costs.sync(now=later + timedelta(minutes=10))
    assert ops == []

    async with clean() as session:
        view = (await session.execute(text("select day, amount_usd from analytics.v_anthropic_costs order by day"))).all()
    assert view == [(date(2026, 8, 1), Decimal("1.000000")), (date(2026, 10, 17), Decimal("2.500000"))]


async def test_broken_key_tells_ops_once_and_keeps_the_data(clean, key, ops):
    ok = Anthropic({"data": [bucket("2026-10-17", item("200"))], "has_more": False})
    await costs.sync(now=NOW, transport=ok.transport, sleep=ok.sleep)
    for minutes in (61, 122):
        bad = Anthropic(403)
        result = await costs.sync(now=NOW + timedelta(minutes=minutes), transport=bad.transport, sleep=bad.sleep)
        assert "failed" in result
    assert len(ops) == 1 and ops[0][2] == "-100" and "не принял админ-ключ" in ops[0][1]
    assert KEY not in ops[0][1]
    assert await rows_in(clean) == [(date(2026, 10, 17), Decimal("2.000000"))]
