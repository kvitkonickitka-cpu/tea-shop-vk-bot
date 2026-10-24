"""Регламенты в Ops: ежедневные контроли, утренний список менеджеру, закрытие месяца, налоги.

Пакет B разбора процессов. Всё — из тика расписания, с отметками «уже
сделано» в heartbeats / client_notices, чтобы сообщение уходило ровно раз:

- **контроли** (`check_controls`): критичные (вручено без закрывающего чека,
  оплачено > 24 ч и не передано перевозчику) — тревога сразу, по разу на
  заказ; сводка всех проверок — в Ops раз в день в 10:00 МСК;
- **реклама за вчера** — напоминание в Ops в 11:00, если кампания идёт, а
  строки в `fin.ad_spend` за вчера нет;
- **утренний список** (`morning_list`) — менеджеру в 10:00: собрать, сдать,
  подтвердить в Ozon, ответить, забрать из пункта выдачи;
- **закрытие месяца** (`month_close`) — 1-го в 10:00 в Ops, автоматические
  пункты ✅/❌ и ручные; повтор 3-го, если что-то ❌;
- **налоговый календарь** (`tax_reminders`) — за 7 дней и за день до срока.

Данных клиентов в сообщениях нет: номера заказов, суммы, счётчики.
Тестовые заказы (`orders.is_test`) в контролях не считаются.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.core import heartbeat, worktime
from app.core.config import settings
from app.core.database import get_session_factory

logger = logging.getLogger(__name__)

SUMMARY_HOUR = 10
ADS_HOUR = 11
MORNING_HOUR = 10
CLOSE_HOUR = 10
LIST_LIMIT = 10
# Утреннее — только утром: после деплоя вечером «сегодня ещё не было» не должно
# слать «дела на день» в 22:35. Пропустили окно — до завтра.
MORNING_WINDOW_HOURS = 3


def _in_window(local: datetime, start: int) -> bool:
    return start <= local.hour < start + MORNING_WINDOW_HOURS


@dataclass
class Check:
    key: str
    title: str
    orders: list[int] = field(default_factory=list)
    critical: bool = False
    note: str = ""
    # None — проверить нечем (нет таблицы, механизм ещё не включён): «нет данных», не ✅.
    known: bool = True

    @property
    def ok(self) -> bool:
        return self.known and not self.orders


async def _rows(sql: str, **params) -> list:
    async with get_session_factory()() as session:
        return (await session.execute(text(sql), params)).all()


async def _exists(name: str) -> bool:
    return bool((await _rows("select to_regclass(:n) is not null", n=name))[0][0])


_REAL = "not o.is_test and o.payment_status = 'succeeded' and o.status not in ('refunded', 'canceled')"


async def collect(now: datetime | None = None) -> list[Check]:
    """Все ежедневные контроли (B1)."""
    now = now or datetime.now(timezone.utc)
    checks = []

    rows = await _rows(
        f"select o.id from orders o where {_REAL} and o.delivered_at is not null and o.delivered_at < :t"
        " and coalesce(o.settlement_receipt_status, '') <> 'succeeded' order by o.id", t=now - timedelta(hours=2))
    note = "" if settings.settlement_receipt_enabled else "закрывающие чеки выключены (SETTLEMENT_RECEIPT_ENABLED)"
    checks.append(Check("delivered_no_receipt", "Вручено, но нет закрывающего чека", [r[0] for r in rows],
                        critical=True, note=note))

    rows = await _rows(
        f"select o.id from orders o where {_REAL} and o.paid_at < :t and o.handed_over_at is null"
        " and o.delivered_at is null and o.not_delivered_at is null order by o.id", t=now - timedelta(hours=24))
    checks.append(Check("paid_not_handed", "Оплачено больше суток назад, а перевозчику не передано",
                        [r[0] for r in rows], critical=True))

    rows = await _rows(
        f"select o.id from orders o where {_REAL} and o.paid_at < :t and o.delivered_at is null"
        " and o.not_delivered_at is null order by o.id", t=now - timedelta(days=10))
    checks.append(Check("paid_not_delivered_10d", "Оплачено больше 10 дней назад и не вручено", [r[0] for r in rows]))

    if await _exists("finance.yk_refunds"):
        rows = await _rows(
            "select coalesce(r.order_id, 0) from finance.yk_refunds r where r.status = 'succeeded'"
            " and coalesce(r.receipt_registration, '') <> 'succeeded' and r.created_at < :t order by 1",
            t=now - timedelta(hours=1))
        checks.append(Check("refund_no_receipt", "Возврат без чека возврата", [r[0] for r in rows]))
    else:
        checks.append(Check("refund_no_receipt", "Возврат без чека возврата", known=False, note="реестра ЮKassa нет"))

    from app.modules.marking import circulation

    stray = await circulation.not_in_circulation()
    checks.append(Check("codes_not_in_circulation", "Код ЧЗ в чеке не введён в оборот", stray))
    return checks


def _orders_text(orders: list[int]) -> str:
    shown = ", ".join(f"№{o}" if o else "без заказа" for o in orders[:LIST_LIMIT])
    return shown + (f" и ещё {len(orders) - LIST_LIMIT}" if len(orders) > LIST_LIMIT else "")


async def _ops(text_: str, *, kind: str | None = None, order_id: int | None = None) -> None:
    from app.messages import manager as manager_messages

    await manager_messages.notify(kind or manager_messages.OPS_CONTROLS, text_, order_id=order_id,
                                  chat_id=settings.telegram_ops_chat_id or None)


async def check_controls(*, now: datetime | None = None, force_summary: bool = False) -> dict:
    """Тик: критичное — сразу (по разу на заказ), сводка — раз в день в 10:00 МСК."""
    from app.messages import client as client_messages

    now = now or datetime.now(timezone.utc)
    if not settings.telegram_ops_chat_id:
        return {"skipped": "нет чата Ops"}
    checks = await collect(now)
    alerted = 0
    for check in checks:
        if not check.critical:
            continue
        for order_id in check.orders:
            if not await client_messages.claim_once(f"control:{check.key}:{order_id}", "ops_control", 0):
                continue
            await _ops(f"🚨 <b>Заказ №{order_id}: {html.escape(check.title.lower())}</b>"
                       + (f"\n{html.escape(check.note)}" if check.note else ""), order_id=order_id)
            alerted += 1
    result = {"тревог": alerted, "проблем": {c.key: len(c.orders) for c in checks if c.orders}}
    local = worktime.to_msk(now)
    if force_summary or (_in_window(local, SUMMARY_HOUR) and not await _done_today("контроли: сводка", local)):
        await _ops(render_summary(checks, local))
        if not force_summary:
            await heartbeat.note("контроли: сводка")
        result["сводка"] = True
    result["реклама"] = await ads_reminder(now=now)
    return result


async def _done_today(name: str, local: datetime) -> bool:
    last = await heartbeat.last_run(name)
    return last is not None and worktime.to_msk(last).date() == local.date()


def render_summary(checks: list[Check], local: datetime) -> str:
    lines = [f"<b>Контроли на {local:%d.%m}</b>"]
    for check in checks:
        if not check.known:
            mark = "⚪"
            tail = f" — {html.escape(check.note)}" if check.note else ""
        elif check.ok:
            mark, tail = "✅", ""
        else:
            mark = "🚨" if check.critical else "❌"
            tail = f": {len(check.orders)} — {_orders_text(check.orders)}"
            if check.note:
                tail += f" ({html.escape(check.note)})"
        lines.append(f"{mark} {html.escape(check.title)}{tail}")
    return "\n".join(lines)


async def ads_reminder(*, now: datetime | None = None, force: bool = False) -> dict:
    """Кампания идёт, а расхода рекламы за вчера в fin.ad_spend нет — напомнить в 11:00."""
    from app.messages import client as client_messages

    now = now or datetime.now(timezone.utc)
    local = worktime.to_msk(now)
    if not force and not _in_window(local, ADS_HOUR):
        return {"skipped": "не время"}
    if not await _exists("fin.ad_spend"):
        return {"skipped": "схемы fin нет"}
    yesterday = local.date() - timedelta(days=1)
    rows = await _rows(
        "select coalesce(fin.inp_date('ad_campaign_start', :d), fin.inp_date('pilot_start_date', :d)),"
        " fin.inp_date('ad_campaign_end', :d), exists (select 1 from fin.ad_spend where date = :d)", d=yesterday)
    start, end, entered = rows[0]
    if start is None or yesterday < start or (end is not None and yesterday > end):
        return {"skipped": "кампания не идёт"}
    if entered:
        return {"ok": True}
    if not force and not await client_messages.claim_once(f"ads_missing:{yesterday}", "ads_missing", 0):
        return {"skipped": "уже напоминали"}
    await _ops(f"📣 <b>Нет расхода рекламы за {yesterday:%d.%m}</b>\nВнесите строку «Реклама за день» на дашборде "
               "«Финансы → Ввод данных»: без неё CAC и ворота пилота считаются неверно.")
    return {"sent": True}


# --- B2: утренний список менеджеру -------------------------------------------------------


async def morning_list(*, now: datetime | None = None, force: bool = False) -> dict:
    """Дела на день — менеджеру в 10:00 МСК. Пустые блоки не показываем."""
    now = now or datetime.now(timezone.utc)
    local = worktime.to_msk(now)
    if not force and (not _in_window(local, MORNING_HOUR) or await _done_today("утренний список", local)):
        return {"skipped": "не время или уже"}
    text_ = await render_morning(now)
    from app.messages import manager as manager_messages

    await manager_messages.notify(manager_messages.MORNING_LIST, text_)
    if not force:
        await heartbeat.note("утренний список")
    return {"sent": True, "text": text_}


async def render_morning(now: datetime) -> str:
    from app.modules.marking import packing

    blocks: list[str] = []

    pack = await _rows(f"select o.id from orders o where {_REAL} and o.packed_at is null and o.handed_over_at is null"
                       " and o.delivered_at is null order by o.paid_at")
    if pack:
        lines = []
        for (order_id,) in pack[:LIST_LIMIT]:
            made = packing.pack_url(order_id, settings.public_base_url) if settings.public_base_url else None
            lines.append(f"• №{order_id}" + (f" — <a href=\"{html.escape(made[0], quote=True)}\">собрать</a>" if made else ""))
        blocks.append("📦 <b>Собрать</b>\n" + "\n".join(lines) + _more(pack))

    hand = await _rows(f"select o.id, o.delivery_method from orders o where {_REAL} and o.packed_at is not null"
                       " and o.handed_over_at is null and o.delivered_at is null order by o.packed_at")
    if hand:
        blocks.append("🚚 <b>Сдать перевозчику</b>\n" + "\n".join(
            f"• №{i} — {'Ozon' if (m or '').startswith('ozon') else 'СДЭК'}" for i, m in hand[:LIST_LIMIT]) + _more(hand))

    from app.core import flags

    if await flags.mode(flags.OZON_AUTO_APPROVE) != flags.ON:
        approve = await _rows(f"select o.id from orders o where {_REAL} and o.ozon_posting is not null"
                               " and o.handed_over_at is null and not (coalesce(o.details, '{}'::jsonb) ? 'ozon_approved_at')"
                              " order by o.id")
        if approve:
            blocks.append("✅ <b>Подтвердить в Ozon</b> (автоподтверждение выключено)\n"
                          + "\n".join(f"• №{i}" for (i,) in approve[:LIST_LIMIT]) + _more(approve))

    questions = await _rows("select e.id, e.peer_id, e.created_at from escalations e where e.resolved_at is null"
                            " order by e.created_at")
    if questions:
        # Вопросы, пришедшие вне рабочего времени, — первыми: клиент ждёт с ночи.
        ordered = sorted(questions, key=lambda q: (worktime.is_working(q[2]), q[2]))
        lines = []
        for _, peer_id, created in ordered[:LIST_LIMIT]:
            waited = _waited(now - created)
            night = "" if worktime.is_working(created) else " 🌙 вне рабочего времени"
            lines.append(f"• <a href=\"https://vk.com/gim{_group()}?sel={peer_id}\">диалог</a> — ждёт {waited}{night}")
        blocks.append("💬 <b>Ответить</b>\n" + "\n".join(lines) + _more(questions))

    pickup = await _rows(f"select o.id, o.storage_until from orders o where {_REAL} and o.at_pickup_at is not null"
                         " and o.delivered_at is null and o.not_delivered_at is null order by o.storage_until nulls last")
    if pickup:
        blocks.append("📍 <b>В пункте выдачи</b>\n" + "\n".join(
            f"• №{i}" + (f" — хранится до {d:%d.%m}" if d else " — срок хранения неизвестен")
            for i, d in pickup[:LIST_LIMIT]) + _more(pickup))

    head = f"☀️ <b>Дела на {worktime.to_msk(now):%d.%m}</b>"
    return head + ("\n\n" + "\n\n".join(blocks) if blocks else "\nДел нет.")


def _more(rows: list) -> str:
    return f"\n…и ещё {len(rows) - LIST_LIMIT}" if len(rows) > LIST_LIMIT else ""


def _group() -> str:
    return "".join(ch for ch in (settings.vk_group_id or "") if ch.isdigit())


def _waited(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours >= 24:
        return f"{hours // 24} дн. {hours % 24} ч"
    return f"{hours} ч {int(delta.total_seconds() % 3600 // 60)} мин"


# --- B3: закрытие месяца ------------------------------------------------------------------


@dataclass
class Item:
    title: str
    ok: bool | None  # None — проверить нечем
    detail: str = ""


async def month_items(month_start: date) -> list[Item]:
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    items: list[Item] = []

    if await _exists("finance.v_cash_summary"):
        rows = await _rows("select check_diff from finance.v_cash_summary where period_type = 'M' and period_start = :m",
                           m=month_start)
        diff = rows[0][0] if rows else None
        items.append(Item("Выписка сходится (check_diff = 0)", None if diff is None else diff == 0,
                          "нет строки за месяц" if diff is None else f"{diff} ₽"))
    else:
        items.append(Item("Выписка сходится (check_diff = 0)", None, "выписки нет"))

    if await _exists("finance.fact_operations"):
        count = (await _rows("select count(*) from finance.fact_operations where article_id = 'tech_unclassified'"
                             " and operation_date >= :s and operation_date < :e", s=month_start, e=month_end))[0][0]
        items.append(Item("«Не разобрано» в выписке = 0", count == 0, str(count)))
    else:
        items.append(Item("«Не разобрано» в выписке = 0", None, "выписки нет"))

    if await _exists("finance.yk_payouts"):
        mismatch = (await _rows("select count(*) from finance.yk_payouts where status = 'mismatch'"
                                " and payout_date >= :s and payout_date < :e", s=month_start, e=month_end))[0][0]
        unpaid = (await _rows(
            "select coalesce(sum(income_amount), 0) from finance.yk_payments where status = 'succeeded' and payout_date is null"
            " and (captured_at at time zone 'Europe/Moscow')::date >= :s and (captured_at at time zone 'Europe/Moscow')::date < :e"
            " and captured_at < now() - interval '5 days'", s=month_start, e=month_end))[0][0]
        items.append(Item("Реестр ЮKassa сверен с выплатами", mismatch == 0 and unpaid <= 0,
                          f"расхождений {mismatch}, не выплачено {unpaid} ₽"))
    else:
        items.append(Item("Реестр ЮKassa сверен с выплатами", None, "реестра нет"))

    no_prepay = (await _rows(
        "select count(*) from orders o where not o.is_test and o.payment_status = 'succeeded'"
        " and o.paid_at >= :s and o.paid_at < :e and coalesce(o.receipt_status, '') <> 'succeeded'",
        s=_msk_start(month_start), e=_msk_start(month_end)))[0][0]
    no_settle = (await _rows(
        f"select count(*) from orders o where {_REAL} and o.delivered_at >= :s and o.delivered_at < :e"
        " and coalesce(o.settlement_receipt_status, '') <> 'succeeded'",
        s=_msk_start(month_start), e=_msk_start(month_end)))[0][0]
    no_refund = 0
    if await _exists("finance.yk_refunds"):
        no_refund = (await _rows(
            "select count(*) from finance.yk_refunds where status = 'succeeded' and created_at >= :s and created_at < :e"
            " and coalesce(receipt_registration, '') <> 'succeeded'",
            s=_msk_start(month_start), e=_msk_start(month_end)))[0][0]
    items.append(Item("Чеки: предоплата у оплаченных, закрывающий у вручённых, у возвратов — чек возврата",
                      no_prepay == 0 and no_settle == 0 and no_refund == 0,
                      f"без чека предоплаты {no_prepay}, без закрывающего {no_settle}, возвратов без чека {no_refund}"))
    from app.modules.marking import circulation

    pair = await circulation.sold_vs_codes(_msk_start(month_start), _msk_start(month_end))
    units, codes_out = pair
    stray = await circulation.not_in_circulation(since=_msk_start(month_start), until=_msk_start(month_end)) or []
    items.append(Item("Выбывшие коды ЧЗ = проданные пачки, все — введены в оборот",
                      units == codes_out and not stray,
                      f"пачек и сетов {units}, кодов в чеках {codes_out}, не в обороте — заказов {len(stray)}"))
    return items


MANUAL = (
    "Инвентаризация: граммы по SKU и пачки — внести «Фасовку» / расхождение",
    "Списания (дегустации, образцы, брак) — отдельной статьёй расхода",
    "Вводные актуальны: курс, ставки эквайринга, тарифы, план рекламы",
)


def _msk_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=worktime.MSK).astimezone(timezone.utc)


def render_close(month_start: date, items: list[Item], *, repeat: bool = False) -> str:
    title = f"🗓 <b>Закрытие месяца {month_start:%m.%Y}</b>" + (" — повтор: есть ❌" if repeat else "")
    lines = [title, "", "<b>Проверено автоматически</b>"]
    for item in items:
        mark = "⚪" if item.ok is None else ("✅" if item.ok else "❌")
        lines.append(f"{mark} {html.escape(item.title)}" + (f" — {html.escape(item.detail)}" if item.detail else ""))
    lines += ["", "<b>Руками</b>"] + [f"☐ {html.escape(m)}" for m in MANUAL]
    return "\n".join(lines)


async def month_close(*, now: datetime | None = None, force: bool = False) -> dict:
    """1-го числа в 10:00 — чек-лист за прошлый месяц; 3-го — повтор, если есть ❌."""
    from app.messages import client as client_messages

    now = now or datetime.now(timezone.utc)
    local = worktime.to_msk(now)
    month_start = (local.date().replace(day=1) - timedelta(days=1)).replace(day=1)
    if not force and (local.hour < CLOSE_HOUR or local.day not in (1, 3)):
        return {"skipped": "не 1-е и не 3-е"}
    items = await month_items(month_start)
    failed = [i for i in items if i.ok is False]
    repeat = local.day == 3
    if not force:
        if repeat and not failed:
            return {"skipped": "к 3-му всё ✅"}
        key = f"month_close:{month_start:%Y-%m}:{local.day}"
        if not await client_messages.claim_once(key, "month_close", 0):
            return {"skipped": "уже отправлен"}
    await _ops(render_close(month_start, items, repeat=repeat and not force))
    return {"sent": True, "❌": len(failed), "месяц": f"{month_start:%Y-%m}"}


# --- B4: налоговый календарь -----------------------------------------------------------------


async def tax_reminders(*, now: datetime | None = None) -> dict:
    """За 7 дней и за день до срока — в Ops, по разу на событие и срок напоминания."""
    from app.messages import client as client_messages

    if not await _exists("fin.tax_calendar"):
        return {"skipped": "схемы fin нет"}
    now = now or datetime.now(timezone.utc)
    today = worktime.to_msk(now).date()
    rows = await _rows("select id, due_date, event, amount_hint from fin.tax_calendar where status = 'ожидает'"
                       " and due_date in (:d7, :d1) order by due_date", d7=today + timedelta(days=7), d1=today + timedelta(days=1))
    sent = 0
    for tax_id, due, event, amount in rows:
        days = (due - today).days
        if not await client_messages.claim_once(f"tax:{tax_id}:{days}", "tax_reminder", 0):
            continue
        hint = f", ориентир {round(amount):,} ₽".replace(",", " ") if amount else ""
        await _ops(f"🧾 <b>{'Завтра' if days == 1 else 'Через неделю'}, {due:%d.%m}: {html.escape(event)}</b>{hint}\n"
                   f"Сделано — <code>/tax {tax_id} оплачено &lt;сумма&gt;</code> или <code>/tax {tax_id} подано</code>.")
        sent += 1
    return {"напоминаний": sent}


async def tax_list() -> list:
    return await _rows("select id, due_date, event, amount_hint, status, paid_amount from fin.tax_calendar"
                       " where status = 'ожидает' or done_on > current_date - 30 order by due_date limit 20")


async def tax_mark(tax_id: int, status: str, amount, *, by: str) -> bool:
    async with get_session_factory()() as session:
        done = await session.execute(text(
            "update fin.tax_calendar set status = :s, paid_amount = coalesce(:a, paid_amount), done_on = current_date,"
            " done_by = :b where id = :i"), {"s": status, "a": amount, "b": by[:120], "i": tax_id})
        await session.commit()
    return bool(done.rowcount)
