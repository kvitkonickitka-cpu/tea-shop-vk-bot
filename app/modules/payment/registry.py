"""Реестр ЮKassa в схеме finance: платежи, возвраты и сверка выплат с выпиской.

Зачем: выписка банка видит только выплаты ЮKassa — нетто, одной суммой за
день (`cfo_in_yookassa`). Сколько заплатил покупатель, сколько удержала
ЮKassa и какой платёж ещё «в пути», знает только сама ЮKassa. Реестр — её
платежи и возвраты как есть, только чтением (GET):

- `finance.yk_payments` — платёж: сумма (`amount`, брутто), сколько получит
  магазин (`income_amount`), комиссия (`fee` = amount − income_amount),
  способ оплаты, когда списаны деньги (`captured_at`), заказ;
- `finance.yk_refunds` — возвраты;
- `finance.yk_payouts` — сверка: каждая выплата ЮKassa из выписки и дни
  платежей, которые в неё вошли.

Сверка: выплата за день — сумма `income_amount` платежей минус возвраты за
один или несколько дней подряд до дня выплаты. Расхождение больше рубля или
платежи, не выплаченные дольше трёх рабочих дней, — ⚠️ в Ops, по одному
разу. Таблицы бот создаёт сам (`ensure_tables`): cashflow их не знает и не
трогает. Тестовые платежи (`test: true`) в реестр не попадают.

Расписание — раз в день после выписки 07:00 (тик), руками —
`scripts/api.sh yookassa/registry` или `/registry` в чате Ops.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from app.core import heartbeat, worktime
from app.core.config import settings
from app.core.database import get_session_factory

logger = logging.getLogger(__name__)

HEARTBEAT = "реестр ЮKassa"
FIRST_DAYS = 120
REFRESH_DAYS = 14
RUN_AFTER_HOUR_MSK = 8  # выписка Т-Банка приходит в 07:00
PAYOUT_TOLERANCE = Decimal("1.00")
UNPAID_BUSINESS_DAYS = 3
_PAGE_LIMIT = 100
_MAX_PAGES = 50

DDL = (
    "create schema if not exists finance",
    """create table if not exists finance.yk_payments (
        id              text primary key,
        status          text not null,
        amount          numeric(14,2) not null,
        income_amount   numeric(14,2),
        fee             numeric(14,2) generated always as (amount - income_amount) stored,
        payment_method  text,
        created_at      timestamptz,
        captured_at     timestamptz,
        order_id        integer,
        refunded_amount numeric(14,2),
        payout_date     date,
        synced_at       timestamptz not null default now()
    )""",
    "comment on table finance.yk_payments is 'Платежи ЮKassa (GET /v3/payments, без тестовых): брутто amount, нетто income_amount, комиссия fee. payout_date — выплата из выписки, в которую вошёл платёж (сверка бота).'",
    "create index if not exists yk_payments_captured_idx on finance.yk_payments (captured_at)",
    "create index if not exists yk_payments_order_idx on finance.yk_payments (order_id)",
    """create table if not exists finance.yk_refunds (
        id           text primary key,
        payment_id   text not null,
        status       text not null,
        amount       numeric(14,2) not null,
        created_at   timestamptz,
        order_id     integer,
        payout_date  date,
        synced_at    timestamptz not null default now()
    )""",
    "comment on table finance.yk_refunds is 'Возвраты ЮKassa (GET /v3/refunds). Удерживаются из следующей выплаты.'",
    """create table if not exists finance.yk_payouts (
        operation_id    text not null,
        split_no        smallint not null default 1,
        payout_date     date not null,
        bank_amount     numeric(14,2) not null,
        days_from       date,
        days_to         date,
        registry_amount numeric(14,2),
        diff            numeric(14,2),
        status          text not null check (status in ('matched', 'mismatch')),
        checked_at      timestamptz not null default now(),
        primary key (operation_id, split_no)
    )""",
    "comment on table finance.yk_payouts is 'Сверка выплат ЮKassa из выписки (cfo_in_yookassa) с реестром: какие дни платежей вошли, расхождение.'",
)


async def ensure_tables(conn) -> None:
    """Таблицы реестра и витрина КУДиР — при старте бота, своей транзакцией."""
    from app.modules.payment import kudir

    for statement in DDL:
        await conn.exec_driver_sql(statement)
    await kudir.ensure_view(conn)


def _money(field) -> Decimal | None:
    if isinstance(field, dict) and field.get("value") not in (None, ""):
        return Decimal(str(field["value"]))
    return None


def _time(raw) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


async def _list(path: str, since: datetime) -> list[dict]:
    """Все объекты списка ЮKassa с created_at ≥ since — по страницам курсора."""
    from app.modules.payment import yookassa_client

    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    items: list[dict] = []
    cursor = None
    for _ in range(_MAX_PAGES):
        query = f"{path}?created_at.gte={stamp}&limit={_PAGE_LIMIT}" + (f"&cursor={cursor}" if cursor else "")
        page = await yookassa_client._call("GET", query)
        items.extend(page.get("items") or [])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return items


async def sync(*, force: bool = False, now: datetime | None = None, days: int | None = None) -> dict:
    """Забрать платежи и возвраты из ЮKassa и сверить с выпиской. Раз в день; force — сейчас."""
    from app.modules.payment import yookassa_client

    if not yookassa_client.is_configured():
        return {"skipped": "ключи ЮKassa не заданы"}
    now = now or datetime.now(timezone.utc)
    last = await heartbeat.last_run(HEARTBEAT)
    if not force:
        local = worktime.to_msk(now)
        if local.hour < RUN_AFTER_HOUR_MSK:
            return {"skipped": "рано: ждём выписку"}
        if last is not None and worktime.to_msk(last).date() == local.date():
            return {"skipped": "сегодня уже"}
    window = days or (FIRST_DAYS if last is None else REFRESH_DAYS)
    since = now - timedelta(days=window)
    payments = [p for p in await _list("/payments", since) if not p.get("test")]
    refunds = await _list("/refunds", since)
    saved = await _store(payments, refunds)
    result = {"дней": window, "платежей": saved[0], "возвратов": saved[1], **await reconcile(now=now)}
    await heartbeat.note(HEARTBEAT)
    return result


async def _store(payments: list[dict], refunds: list[dict]) -> tuple[int, int]:
    async with get_session_factory()() as session:
        conn = await session.connection()
        for statement in DDL:
            await conn.exec_driver_sql(statement)
        for p in payments:
            await session.execute(text(
                "insert into finance.yk_payments (id, status, amount, income_amount, payment_method, created_at,"
                " captured_at, order_id, refunded_amount, synced_at)"
                " values (:id, :status, :amount, :income, :method, :created, :captured,"
                "  (select order_id from order_payments where payment_id = :id), :refunded, now())"
                " on conflict (id) do update set status = excluded.status, amount = excluded.amount,"
                "  income_amount = excluded.income_amount, payment_method = excluded.payment_method,"
                "  captured_at = excluded.captured_at, order_id = coalesce(excluded.order_id, finance.yk_payments.order_id),"
                "  refunded_amount = excluded.refunded_amount, synced_at = now()"
            ), {
                "id": p["id"], "status": p.get("status") or "", "amount": _money(p.get("amount")) or Decimal(0),
                "income": _money(p.get("income_amount")),
                "method": (p.get("payment_method") or {}).get("type"),
                "created": _time(p.get("created_at")), "captured": _time(p.get("captured_at")),
                "refunded": _money(p.get("refunded_amount")),
            })
        for r in refunds:
            await session.execute(text(
                "insert into finance.yk_refunds (id, payment_id, status, amount, created_at, order_id, synced_at)"
                " values (:id, :payment, :status, :amount, :created,"
                "  (select order_id from order_payments where payment_id = :payment), now())"
                " on conflict (id) do update set status = excluded.status, amount = excluded.amount,"
                "  order_id = coalesce(excluded.order_id, finance.yk_refunds.order_id), synced_at = now()"
            ), {
                "id": r["id"], "payment": r.get("payment_id") or "", "status": r.get("status") or "",
                "amount": _money(r.get("amount")) or Decimal(0), "created": _time(r.get("created_at")),
            })
        await session.commit()
    return len(payments), len(refunds)


def _business_days_between(start: date, end: date) -> int:
    """Рабочих дней (пн–пт) после start по end включительно. Праздники не учитываются."""
    days, cursor = 0, start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


async def reconcile(*, now: datetime | None = None) -> dict:
    """Сопоставить выплаты ЮKassa в выписке с днями платежей реестра. Пересчитывается целиком."""
    now = now or datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        has_bank = (await session.execute(text("select to_regclass('finance.fact_operations') is not null"))).scalar()
        if not has_bank:
            return {"сверка": "выписки нет (finance.fact_operations)"}
        net_rows = (await session.execute(text(
            "select d, sum(v) from ("
            " select (captured_at at time zone 'Europe/Moscow')::date as d, income_amount as v from finance.yk_payments"
            "  where status = 'succeeded' and income_amount is not null and captured_at is not null"
            " union all"
            " select (created_at at time zone 'Europe/Moscow')::date, -amount from finance.yk_refunds"
            "  where status = 'succeeded' and created_at is not null"
            ") x group by d having sum(v) <> 0 order by d"
        ))).all()
        payouts = (await session.execute(text(
            "select operation_id, split_no, operation_date, amount from finance.fact_operations"
            " where article_id = 'cfo_in_yookassa' and direction = 'in' order by operation_date, operation_id, split_no"
        ))).all()

    days = [(d, Decimal(v)) for d, v in net_rows]
    matched_days: dict[date, date] = {}
    results = []
    i = 0
    for operation_id, split_no, payout_date, bank in payouts:
        bank = Decimal(bank)
        cum, j, hit = Decimal(0), i, None
        while j < len(days) and days[j][0] < payout_date:
            cum += days[j][1]
            j += 1
            if abs(cum - bank) <= PAYOUT_TOLERANCE:
                hit = j
                break
        if hit is not None:
            for d, _ in days[i:hit]:
                matched_days[d] = payout_date
            results.append((operation_id, split_no, payout_date, bank, days[i][0], days[hit - 1][0], cum, bank - cum,
                             "matched"))
            i = hit
        else:
            before = sum((v for d, v in days[i:] if d < payout_date), Decimal(0))
            results.append((operation_id, split_no, payout_date, bank, None, None, before, bank - before, "mismatch"))

    today = worktime.to_msk(now).date()
    unpaid = [(d, v) for d, v in days[i:] if d not in matched_days]
    overdue = [(d, v) for d, v in unpaid if _business_days_between(d, today) > UNPAID_BUSINESS_DAYS]

    async with get_session_factory()() as session:
        await session.execute(text("update finance.yk_payments set payout_date = null where payout_date is not null"))
        await session.execute(text("update finance.yk_refunds set payout_date = null where payout_date is not null"))
        for d, payout_date in matched_days.items():
            await session.execute(text(
                "update finance.yk_payments set payout_date = :p where status = 'succeeded'"
                " and (captured_at at time zone 'Europe/Moscow')::date = :d"), {"p": payout_date, "d": d})
            await session.execute(text(
                "update finance.yk_refunds set payout_date = :p where status = 'succeeded'"
                " and (created_at at time zone 'Europe/Moscow')::date = :d"), {"p": payout_date, "d": d})
        await session.execute(text("delete from finance.yk_payouts"))
        for row in results:
            await session.execute(text(
                "insert into finance.yk_payouts (operation_id, split_no, payout_date, bank_amount, days_from, days_to,"
                " registry_amount, diff, status) values (:o, :s, :p, :b, :f, :t, :r, :diff, :st)"
            ), dict(zip(("o", "s", "p", "b", "f", "t", "r", "diff", "st"), row)))
        await session.commit()

    mismatched = [r for r in results if r[8] == "mismatch"]
    await _alert(mismatched, overdue)
    return {
        "выплат": len(results), "сошлось": len(results) - len(mismatched), "расхождений": len(mismatched),
        "в пути, ₽": str(sum((v for _, v in unpaid), Decimal(0))),
        "не выплачено дольше 3 рабочих дней": len(overdue),
    }


async def _alert(mismatched: list, overdue: list) -> None:
    from app.messages import client as client_messages
    from app.messages import manager as manager_messages

    for operation_id, _, payout_date, bank, _, _, registry, diff, _ in mismatched:
        if not await client_messages.claim_once(f"yk_payout:{operation_id}", "yk_payout_mismatch", 0):
            continue
        await manager_messages.notify(
            manager_messages.YK_REGISTRY,
            f"⚠️ <b>Выплата ЮKassa {payout_date:%d.%m}: {bank} ₽ не сходится с реестром</b>\n"
            f"Платежи минус возвраты до этого дня: {registry} ₽, разница {diff} ₽.\n"
            "Проверьте реестр выплат в кабинете ЮKassa: удержание, возврат или платёж вне реестра.",
            chat_id=settings.telegram_ops_chat_id or None,
        )
    for day, amount in overdue:
        if not await client_messages.claim_once(f"yk_unpaid:{day.isoformat()}", "yk_unpaid", 0):
            continue
        await manager_messages.notify(
            manager_messages.YK_REGISTRY,
            f"⚠️ <b>ЮKassa: платежи за {day:%d.%m} на {amount} ₽ не выплачены больше {UNPAID_BUSINESS_DAYS} рабочих дней</b>\n"
            "В выписке нет выплаты, которая бы их покрыла. Проверьте выплаты в кабинете ЮKassa.",
            chat_id=settings.telegram_ops_chat_id or None,
        )
