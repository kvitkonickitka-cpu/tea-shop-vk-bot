"""Сессии диалога, источник клиента, консультация, расход Claude и шаги воронки (пакет A, A1–A4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.client_key import client_key
from app.core.config import settings
from app.messages import funnel
from app.modules.analytics import service as analytics
from app.modules.dialog import consultation, sessions
from app.modules.ops import anthropic_costs, journal
from app.modules.orders.models import Order

AD, FRIEND, PLAIN, TESTER = 7401, 7402, 7403, 7404
T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)
TOKENS = ", input_tokens, output_tokens, cache_read_tokens, cache_write_tokens"
ZEROS = ", 0, 0, 0, 0"


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setattr(settings, "client_key_secret", "s3cret")
    monkeypatch.setattr(settings, "test_vk_ids", str(TESTER))
    monkeypatch.setattr(analytics, "_test_ids", None)
    journal._USAGE.clear()
    yield
    journal._USAGE.clear()


async def select(db, sql: str, **params) -> list[dict]:
    async with db() as session:
        return [dict(r._mapping) for r in await session.execute(text(sql), params)]


def test_source_from_ref():
    assert sessions.classify(None) == "organic"
    assert sessions.classify("  ") == "organic"
    assert sessions.classify("test_smoke") == "ad:test_smoke"
    assert sessions.classify("r_chai-ab12-cd34") == "referral:CHAI-AB12-CD34"
    assert sessions.classify("R_") == "referral:unknown"
    # Из ссылки берём только безопасные символы и не длиннее 100.
    assert sessions.classify("autumn<script>") == "ad:autumnscript"
    assert len(sessions.classify("x" * 500)) == len("ad:") + 100


async def test_first_source_is_not_overwritten(clean):
    await analytics.ensure_client(AD, ref="autumn", ref_source="post")
    await analytics.ensure_client(AD, ref="winter")
    [row] = await select(clean, "select first_source, ref from clients where peer_id = :p", p=AD)
    assert row == {"first_source": "ad:autumn", "ref": "autumn"}


async def test_session_lifecycle(clean):
    first = await sessions.touch(AD, ref="autumn", at=T0)
    assert await sessions.touch(AD, at=T0 + timedelta(hours=5)) == first
    # Сутки тишины — новая сессия, уже без метки: last touch — органика.
    second = await sessions.touch(AD, at=T0 + timedelta(hours=30))
    assert second != first
    # Оплата закрывает сессию: следующее сообщение — новая, даже через минуту.
    await funnel.record(AD, "payment_succeeded", order_id=1, at=T0 + timedelta(hours=31))
    third = await sessions.touch(AD, ref="r_abc", at=T0 + timedelta(hours=31, minutes=1))
    assert len({first, second, third}) == 3
    rows = await select(clean, "select id, source, close_reason from dialog_sessions where peer_id = :p order by id", p=AD)
    assert [(r["source"], r["close_reason"]) for r in rows] == [
        ("ad:autumn", None), ("organic", "payment"), ("referral:ABC", None)]
    # Событие воронки получает последнюю сессию клиента.
    [paid] = await select(clean, "select dialog_id from funnel_events where event = 'payment_succeeded'")
    assert paid["dialog_id"] == second


async def test_own_mark_overrides_sources_in_views(clean):
    await analytics.ensure_client(AD, ref="autumn")
    await sessions.touch(AD, ref="autumn")
    await analytics.ensure_client(PLAIN)
    await sessions.touch(PLAIN)
    assert await sessions.mark_own(AD, by="test")
    clients = {r["client_key"]: r for r in await select(clean, "select * from analytics.v_clients")}
    assert clients[client_key(AD)]["first_source"] == "own" and clients[client_key(AD)]["is_own"]
    assert clients[client_key(PLAIN)]["first_source"] == "organic"
    dialogs = {r["client_key"]: r for r in await select(clean, "select * from analytics.v_dialogs")}
    assert dialogs[client_key(AD)]["session_source_kind"] == "own"
    assert dialogs[client_key(PLAIN)]["session_source"] == "organic"


async def test_test_accounts_stay_out_of_dialog_views(clean):
    await analytics.ensure_client(TESTER, ref="test_smoke")
    await sessions.touch(TESTER, ref="test_smoke")
    assert await select(clean, "select * from analytics.v_dialogs") == []


def _response(**usage):
    tokens = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, **usage}
    return SimpleNamespace(model="claude-sonnet-5-5", stop_reason="end_turn", usage=SimpleNamespace(**tokens))


async def test_llm_usage_knows_dialog_stage_and_purpose(clean):
    dialog = await sessions.touch(AD)
    with journal.llm_scope(peer_id=AD, dialog_id=dialog, stage=consultation.CONSULTATION, purpose="sales_bot"):
        journal.note_llm_usage("ход диалога", "claude-sonnet-5-5", _response())
        journal.update_llm_scope(stage=consultation.RECIPIENT, order_id=None)
        journal.note_llm_usage("ход диалога", "claude-sonnet-5-5", _response())
    journal.note_llm_usage("ассистент Ops", "claude-sonnet-5-5", _response())
    with journal.llm_scope(peer_id=AD):
        journal.note_llm_usage("мини-отчёт по диалогу", "claude-sonnet-5-5", _response())
    await journal.flush()
    rows = await select(clean, "select client_key, dialog_id, stage, purpose from llm_usage order by id")
    assert rows == [
        {"client_key": client_key(AD), "dialog_id": dialog, "stage": "consultation", "purpose": "sales_bot"},
        {"client_key": client_key(AD), "dialog_id": dialog, "stage": "recipient_set", "purpose": "sales_bot"},
        {"client_key": None, "dialog_id": None, "stage": None, "purpose": "ops_assistant"},
        {"client_key": client_key(AD), "dialog_id": None, "stage": None, "purpose": "labeling"},
    ]


async def _usd_rub(db, value: str) -> None:
    async with db() as session:
        await session.execute(text(
            "insert into ref_settings (key, valid_from, name, value) values ('usd_rub', '2026-01-01', 'Курс', :v)"
        ), {"v": Decimal(value)})
        await session.commit()


async def _paid_order(db, peer_id: int, created_at: datetime) -> int:
    async with db() as session:
        row = Order(peer_id=peer_id, items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 900}],
                    items_total=900, delivery_cost=0, total=900, delivery_method="ozon_pvz", status="confirmed",
                    payment_status="succeeded", details={}, created_at=created_at, paid_at=created_at)
        session.add(row)
        await session.commit()
        return row.id


async def test_rubles_per_dialog_paid_order_and_new_buyer(clean):
    await _usd_rub(clean, "100")
    for peer in (AD, PLAIN):
        await analytics.ensure_client(peer)
    bought = await sessions.touch(AD, at=T0)
    browsed = await sessions.touch(PLAIN, at=T0)
    async with clean() as session:
        await session.execute(text(
            "insert into llm_usage (at, operation, model, cost_usd, dialog_id" + TOKENS + ") values "
            "(:t, 'ход диалога', 'm', 0.30, :a" + ZEROS + "), (:t, 'ход диалога', 'm', 0.10, :b" + ZEROS + ")"
        ), {"t": T0, "a": bought, "b": browsed})
        await session.commit()
    order_id = await _paid_order(clean, AD, T0 + timedelta(minutes=30))
    await funnel.record(AD, "payment_succeeded", order_id=order_id, at=T0 + timedelta(minutes=31))
    [month] = await select(clean, "select * from analytics.v_llm_cost_by_dialog")
    assert month["dialogs"] == 2 and month["paid_orders"] == 1 and month["new_buyers"] == 1
    assert month["cost_rub"] == Decimal("40.00")
    assert month["rub_per_dialog"] == Decimal("20.00")
    # Весь расход месяца — на оплаты: диалог без покупки входит в цену покупки.
    assert month["rub_per_paid_order"] == Decimal("40.00")
    assert month["rub_per_new_buyer"] == Decimal("40.00")
    usage = await select(clean, "select cost_rub from analytics.v_llm_usage order by cost_rub")
    assert [u["cost_rub"] for u in usage] == [Decimal("10.00"), Decimal("30.00")]


async def test_no_rate_means_no_rubles(clean):
    await analytics.ensure_client(AD)
    dialog = await sessions.touch(AD, at=T0)
    async with clean() as session:
        await session.execute(text(
            "insert into llm_usage (at, operation, model, cost_usd, dialog_id" + TOKENS + ") values (:t, 'ход диалога', 'm', 0.3, :d" + ZEROS + ")"
        ), {"t": T0, "d": dialog})
        await session.commit()
    [month] = await select(clean, "select * from analytics.v_llm_cost_by_dialog")
    assert month["cost_usd"] == Decimal("0.3000") and month["cost_rub"] is None


# --- консультация -------------------------------------------------------------------


def test_status_questions_are_recognised():
    for question in ("Где мой заказ?", "когда придёт посылка", "дайте трек", "Пришла ли посылка?"):
        assert consultation.is_status_question(question), question
    for question in ("Посоветуйте улун", "что взять к сладкому?", "Сколько стоит шу пуэр"):
        assert not consultation.is_status_question(question), question


async def _events(db, peer_id) -> list[str]:
    return [r["event"] for r in await select(db, "select event from funnel_events where peer_id = :p order by id", p=peer_id)]


async def test_consultation_started_once_and_qualified_by_catalog_name(clean, monkeypatch):
    from app.modules.catalog import service as catalog_service

    monkeypatch.setattr(catalog_service, "load_items", lambda: [
        {"name": "Те Гуань Инь 100 г", "price": 900, "in_stock": True}])
    await analytics.ensure_client(PLAIN)
    dialog = await sessions.touch(PLAIN)
    await consultation.after_turn(PLAIN, dialog, had_draft=False, had_invoice=False,
                                  text="Посоветуйте чай", reply="Расскажите, что любите?")
    await consultation.after_turn(PLAIN, dialog, had_draft=False, had_invoice=False,
                                  text="цветочное", reply="Попробуйте Те Гуань Инь — цветочный улун.")
    await consultation.after_turn(PLAIN, dialog, had_draft=False, had_invoice=False,
                                  text="ещё", reply="Те Гуань Инь хорош и вторым проливом.")
    assert await _events(clean, PLAIN) == ["consult_started", "consult_qualified"]
    [event] = await select(clean, "select data, dialog_id from funnel_events where event = 'consult_qualified'")
    assert event == {"data": {"items": ["Те Гуань Инь 100 г"]}, "dialog_id": dialog}


async def test_no_consultation_with_draft_or_about_parcel(clean):
    await analytics.ensure_client(AD)
    dialog = await sessions.touch(AD)
    await consultation.after_turn(AD, dialog, had_draft=True, had_invoice=False, text="Москва", reply="Пункты…")
    await consultation.after_turn(AD, dialog, had_draft=False, had_invoice=True, text="оплачу", reply="Жду")
    assert await _events(clean, AD) == []
    # Вопрос о посылке при посылке в пути — не консультация.
    await _paid_order(clean, AD, T0)
    await consultation.after_turn(AD, dialog, had_draft=False, had_invoice=False,
                                  text="Где моя посылка?", reply="Посылка в пути.")
    assert await _events(clean, AD) == []
    # Тот же вопрос без посылки в пути — консультация (клиент спрашивает до покупки).
    await analytics.ensure_client(PLAIN)
    other = await sessions.touch(PLAIN)
    await consultation.after_turn(PLAIN, other, had_draft=False, had_invoice=False,
                                  text="Когда придёт, если закажу сегодня?", reply="Через 5 дней.")
    assert await _events(clean, PLAIN) == ["consult_started"]


def test_funnel_stage_of_turn():
    draft = SimpleNamespace(details={}, delivery_method=None)
    assert consultation.funnel_stage(None, None) == "consultation"
    assert consultation.funnel_stage(None, SimpleNamespace(id=5)) == "invoice"
    assert consultation.funnel_stage(draft, None) == "draft_created"
    draft.delivery_method = "ozon_pvz"
    assert consultation.funnel_stage(draft, None) == "delivery_quoted"
    draft.details["ozon_point_id"] = 1
    assert consultation.funnel_stage(draft, None) == "point_chosen"
    draft.details["recipient_name"] = "[NAME_1]"
    assert consultation.funnel_stage(draft, None) == "recipient_set"
    assert consultation.open_order_id(SimpleNamespace(details={"order_id": 12}), None) == 12


# --- шаги воронки -------------------------------------------------------------------


async def _walk(db, peer_id, start, steps):
    await analytics.ensure_client(peer_id)
    await sessions.touch(peer_id, at=start)
    for minutes, event in steps:
        await funnel.record(peer_id, event, order_id=1 if event.startswith(("invoice", "payment")) else None,
                            at=start + timedelta(minutes=minutes))


async def test_funnel_steps_reached_dropped_and_times(clean):
    await _walk(clean, AD, T0, [(0, "consult_started"), (10, "draft_created"), (12, "delivery_quoted"),
                                (20, "point_chosen"), (25, "recipient_set"), (26, "invoice_auto"),
                                (36, "payment_succeeded")])
    await _walk(clean, PLAIN, T0, [(0, "consult_started"), (30, "draft_created")])
    steps = {(r["session_source_kind"], r["step"]): r for r in await select(clean, "select * from analytics.v_funnel_steps")}
    total = steps[("all", "consult_started")]
    assert total["reached"] == 2 and total["dropped"] == 0
    assert total["median_minutes_to_next"] == Decimal("20.0")
    draft = steps[("all", "draft_created")]
    assert draft["reached"] == 2 and draft["dropped"] == 1 and draft["drop_pct"] == Decimal("50.0")
    assert steps[("organic", "payment_succeeded")]["reached"] == 1
    # Тот, кто не дошёл до шага, на нём и не отвалился.
    assert steps[("all", "invoice")]["reached"] == 1 and steps[("all", "invoice")]["dropped"] == 0


async def test_invoice_timing_and_expired_share(clean):
    async with clean() as session:
        session.add(Order(id=1, peer_id=AD, items=[], items_total=900, delivery_cost=0, total=900,
                          delivery_method="ozon_pvz", status="payment_expired", payment_status="canceled",
                          details={}))
        await session.commit()
    await _walk(clean, AD, T0, [(0, "draft_created"), (45, "invoice_auto"), (110, "invoice_expired"),
                                (120, "invoice_confirmed")])
    [row] = await select(clean, "select * from analytics.v_invoice_timing")
    assert row["minutes_draft_to_invoice"] == Decimal("45.0")
    assert row["invoice_attempts"] == 2 and row["had_expired"] and not row["is_paid"]
    assert row["session_source_kind"] == "organic"


# --- сверка со счётом Anthropic -------------------------------------------------------


async def test_reconcile_warns_once_when_bill_differs(clean, monkeypatch):
    sent = []

    async def notify(kind, text_, **kwargs):
        sent.append(text_)
        return True

    from app.messages import manager as manager_messages

    monkeypatch.setattr(manager_messages, "notify", notify)
    async with clean() as session:
        await session.execute(text(
            "insert into llm_usage (at, operation, model, cost_usd" + TOKENS + ") values ('2026-10-01 00:00+00', 'ход диалога', 'm', 10" + ZEROS + ")"))
        await session.execute(text(
            "insert into anthropic_costs (day, amount_usd) values ('2026-10-10', 10.3), ('2026-10-11', 1.0)"))
        await session.commit()
    now = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
    assert (await anthropic_costs.reconcile_month(now=datetime(2026, 11, 2, tzinfo=timezone.utc)))["skipped"]
    result = await anthropic_costs.reconcile_month(now=now)
    assert result["расхождение, %"] == "11.5" and result["sent"]
    again = await anthropic_costs.reconcile_month(now=now)
    assert again["skipped"] == "уже сообщали"
    assert len(sent) == 1 and "01.10–31.10" in sent[0]


async def test_reconcile_quiet_within_tolerance(clean):
    async with clean() as session:
        await session.execute(text(
            "insert into llm_usage (at, operation, model, cost_usd" + TOKENS + ") values ('2026-10-01 00:00+00', 'ход диалога', 'm', 10" + ZEROS + ")"))
        await session.execute(text("insert into anthropic_costs (day, amount_usd) values ('2026-10-10', 10.4)"))
        await session.commit()
    result = await anthropic_costs.reconcile_month(now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    assert result["ok"] is True


async def test_reconcile_only_days_the_bot_tracked(clean, monkeypatch):
    """Учёт бота начался 18.10.2026: сентябрь не сверяем, октябрь — с 19-го (первый полный день)."""
    sent = []

    async def notify(kind, text_, **kwargs):
        sent.append(text_)
        return True

    from app.messages import manager as manager_messages

    monkeypatch.setattr(manager_messages, "notify", notify)
    async with clean() as session:
        await session.execute(text(
            "insert into llm_usage (at, operation, model, cost_usd" + TOKENS + ") values "
            "('2026-10-18 15:00+00', 'ход диалога', 'm', 5" + ZEROS + "), "
            "('2026-10-20 12:00+00', 'ход диалога', 'm', 10" + ZEROS + ")"))
        await session.execute(text(
            "insert into anthropic_costs (day, amount_usd) values ('2026-09-15', 2.33), "
            "('2026-10-05', 3.0), ('2026-10-18', 5.0), ('2026-10-20', 10.1)"))
        await session.commit()
    september = await anthropic_costs.reconcile_month(now=datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc))
    assert september["skipped"] == "бот не вёл учёт ни одного полного дня месяца"
    october = await anthropic_costs.reconcile_month(now=datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc))
    assert october["дни"] == "19.10–31.10" and october["ok"] is True
    assert sent == []
