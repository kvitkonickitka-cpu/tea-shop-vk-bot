"""Счёт Anthropic по дням — отчёт о расходах (Admin API `cost_report`).

`llm_usage` — оценка самого бота по ценам; здесь — то, что Anthropic выставит
к оплате, по всей организации (бот, ассистент Ops, консоль, всё остальное).
Читается админ-ключом `ANTHROPIC_ADMIN_KEY` через тот же адрес, что и бот
(`ANTHROPIC_BASE_URL` — прокси на Aeza: из РФ api.anthropic.com не отвечает).

Как устроено:

- раз в час из тика; руками — `scripts/api.sh anthropic/costs`;
- первый раз — последние 90 дней, дальше — последние 7: Anthropic уточняет
  суммы задним числом, а данные за сегодня приходят с задержкой минут в пять;
- прочитанные дни заменяются целиком, одной транзакцией: строки отчёта без
  своего id, и «обновить строку» нечем — вместо этого день как есть;
- сумма в отчёте — в центах десятичной строкой («123.45» = $1,2345); у нас —
  в долларах;
- ключ — с правами на всю организацию: в лог, в ошибки и в Ops-чат не
  попадает ни он, ни его кусок. Неверный или отозванный ключ (401/403) —
  одно сообщение в Ops-чат; временное (429, 5xx, сеть) — повтор, потом
  следующий тик молча.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import delete

from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.ops.models import AnthropicCost, AnthropicCostSync

logger = logging.getLogger(__name__)

INTERVAL = timedelta(hours=1)
FIRST_DAYS = 90
REFRESH_DAYS = 7
_PATH = "/v1/organizations/cost_report"
_TIMEOUT_SECONDS = 20
_RETRY_DELAYS = (2.0, 5.0)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_PAGES = 20  # 31 день на страницу — с запасом на 90 дней и группировку


class CostReportError(RuntimeError):
    """Отчёт не прочитан. Текст — для человека, без ключа."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def _base_url() -> str:
    return (settings.anthropic_base_url or "https://api.anthropic.com").rstrip("/")


def _usd(amount) -> Decimal:
    """«123.45» центов → $1.2345."""
    try:
        return (Decimal(str(amount)) / 100).quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError):
        raise CostReportError(f"сумма «{amount}» в отчёте — не число") from None


async def fetch(start: date, end: date, *, transport: httpx.AsyncBaseTransport | None = None,
                sleep=asyncio.sleep) -> list[dict]:
    """Строки отчёта за дни [start, end): по одной на день × статью расходов."""
    key = (settings.anthropic_admin_key or "").strip()
    if not key:
        raise CostReportError("не задан ANTHROPIC_ADMIN_KEY")
    params = {
        "starting_at": datetime.combine(start, time(), timezone.utc).isoformat().replace("+00:00", "Z"),
        "ending_at": datetime.combine(end, time(), timezone.utc).isoformat().replace("+00:00", "Z"),
        "bucket_width": "1d", "group_by[]": "description", "limit": 31,
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "teashop-bot/1.0"}
    rows: list[dict] = []
    page = None
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=transport) as client:
        for _ in range(_MAX_PAGES):
            query = {**params, **({"page": page} if page else {})}
            data = await _get(client, query, headers, sleep)
            for bucket in data.get("data") or []:
                day = datetime.fromisoformat(str(bucket["starting_at"]).replace("Z", "+00:00")).date()
                for item in bucket.get("results") or []:
                    if (item.get("currency") or "USD") != "USD":
                        raise CostReportError(f"валюта отчёта {item.get('currency')} — ждали USD")
                    rows.append({
                        "day": day, "description": item.get("description"), "model": item.get("model"),
                        "cost_type": item.get("cost_type"), "token_type": item.get("token_type"),
                        "service_tier": item.get("service_tier"), "amount_usd": _usd(item.get("amount")),
                    })
            if not data.get("has_more"):
                return rows
            page = data.get("next_page")
            if not page:
                return rows
    raise CostReportError(f"отчёт не уместился в {_MAX_PAGES} страниц")


async def _get(client: httpx.AsyncClient, query: dict, headers: dict, sleep) -> dict:
    attempt = 0
    while True:
        try:
            response = await client.get(_base_url() + _PATH, params=query, headers=headers)
        except httpx.HTTPError as error:
            status, problem = None, type(error).__name__
        else:
            status, problem = response.status_code, f"HTTP {response.status_code}"
            if status == 200:
                return response.json()
            if status in (401, 403):
                raise CostReportError(
                    f"Anthropic не принял админ-ключ (HTTP {status}): ключ отозван, не админский "
                    "или учётная запись не организация — нужен новый ANTHROPIC_ADMIN_KEY"
                )
            if status not in _RETRY_STATUSES:
                # Текст ответа Anthropic — без ключа, но обрезаем: он про запрос, не про нас.
                raise CostReportError(f"отчёт о расходах: HTTP {status} — {response.text[:200]}")
        if attempt >= len(_RETRY_DELAYS):
            raise CostReportError(f"отчёт о расходах недоступен ({problem})", transient=True)
        delay = _RETRY_DELAYS[attempt]
        attempt += 1
        logger.warning("Отчёт о расходах Anthropic: %s, повтор через %.0f с", problem, delay)
        await sleep(delay)


async def _tell_ops(problem: str) -> None:
    from app.messages import client as client_messages
    from app.messages import manager as manager_messages

    if not await client_messages.claim_once(f"anthropic_costs:{problem[:80]}", "anthropic_costs_problem", 0):
        return
    await manager_messages.notify(
        manager_messages.ANTHROPIC_COSTS,
        f"⚠️ <b>Счёт Anthropic не читается</b>\n{problem}\nВ «Сводке» остаются прежние суммы.",
        chat_id=settings.telegram_ops_chat_id or None,
    )


async def sync(*, force: bool = False, now: datetime | None = None,
               transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep) -> dict:
    """Прочитать счёт Anthropic и заменить им прочитанные дни в базе."""
    if not (settings.anthropic_admin_key or "").strip():
        return {"skipped": "не задан ANTHROPIC_ADMIN_KEY"}
    now = now or datetime.now(timezone.utc)
    session_factory = get_session_factory()
    async with session_factory() as session:
        state = await session.get(AnthropicCostSync, 1)
        if not force and state and state.checked_at and now - state.checked_at < INTERVAL:
            return {"skipped": "читали меньше часа назад"}
        first = state is None or state.synced_at is None

    start = now.date() - timedelta(days=(FIRST_DAYS if first else REFRESH_DAYS) - 1)
    end = now.date() + timedelta(days=1)  # сегодняшний день целиком: бакеты «до» ending_at
    try:
        rows = await fetch(start, end, transport=transport, sleep=sleep)
    except CostReportError as error:
        async with session_factory() as session:
            state = await session.get(AnthropicCostSync, 1) or AnthropicCostSync(id=1)
            state.checked_at = now
            state.last_error = str(error)[:500]
            session.add(state)
            await session.commit()
        if error.transient:
            logger.warning("Счёт Anthropic не прочитали: %s", error)
        else:
            logger.error("Счёт Anthropic не прочитать: %s", error)
            await _tell_ops(str(error))
        return {"failed": str(error)}

    async with session_factory() as session:
        await session.execute(delete(AnthropicCost).where(AnthropicCost.day >= start, AnthropicCost.day < end))
        session.add_all(AnthropicCost(**row, synced_at=now) for row in rows)
        state = await session.get(AnthropicCostSync, 1) or AnthropicCostSync(id=1)
        state.checked_at = now
        state.synced_at = now
        state.last_error = None
        session.add(state)
        await session.commit()
    total = sum((row["amount_usd"] for row in rows), Decimal(0))
    logger.info("Счёт Anthropic: %s–%s, строк %s, $%s", start, end, len(rows), total)
    return {"дни": f"{start} — {end - timedelta(days=1)}", "строк": len(rows), "итого, $": str(total)}


# --- сверка учёта бота со счётом за месяц -----------------------------------------
# Счёт за прошлый месяц Anthropic уточняет несколько дней — сверяем с третьего
# числа. Расхождение больше ANTHROPIC_RECONCILE_TOLERANCE — ⚠️ в Ops, один раз за
# месяц (журнал client_notices). Счёт — по всей организации, учёт бота — только
# его запросы: постоянное расхождение значит, что ключом пользуется кто-то ещё
# или в llm_prices устарели цены.
RECONCILE_FROM_DAY = 3


def _previous_month(today: date) -> tuple[date, date]:
    first = today.replace(day=1)
    start = (first - timedelta(days=1)).replace(day=1)
    return start, first


async def reconcile_month(*, now: datetime | None = None, force: bool = False) -> dict:
    """Сверить сумму llm_usage со счётом Anthropic за прошлый месяц (UTC, как у Anthropic)."""
    from sqlalchemy import text

    now = now or datetime.now(timezone.utc)
    if now.day < RECONCILE_FROM_DAY and not force:
        return {"skipped": "сверяем с третьего числа"}
    start, end = _previous_month(now.date())
    month = start.strftime("%Y-%m")
    async with get_session_factory()() as session:
        first_noted = (await session.execute(text("select min(at) from llm_usage"))).scalar()
    # Учёт расхода бот ведёт с 18.10.2026 (llm_usage): дни до первой записи
    # сравнивать не с чем — счёт за них был бы «расхождением на 100 %».
    if first_noted is None:
        return {"месяц": month, "skipped": "учёта расхода у бота ещё нет"}
    # Первый полный день учёта: день первой записи — неполный (сутки по UTC, как у Anthropic).
    first_utc = first_noted.astimezone(timezone.utc)
    first_full_day = first_utc.date() if first_utc.time() == time() else first_utc.date() + timedelta(days=1)
    covered_from = max(start, first_full_day)
    if covered_from >= end:
        return {"месяц": month, "skipped": "бот не вёл учёт ни одного полного дня месяца"}
    start = covered_from
    lo, hi = datetime.combine(start, time(), timezone.utc), datetime.combine(end, time(), timezone.utc)
    async with get_session_factory()() as session:
        bot = (await session.execute(text(
            "select coalesce(sum(cost_usd), 0) from llm_usage where at >= :lo and at < :hi"
        ), {"lo": lo, "hi": hi})).scalar()
        bill_days = (await session.execute(text(
            "select count(distinct day), coalesce(sum(amount_usd), 0) from anthropic_costs where day >= :s and day < :e"
        ), {"s": start, "e": end})).one()
    days, bill = int(bill_days[0] or 0), Decimal(bill_days[1] or 0)
    bot = Decimal(bot or 0)
    period = f"{start:%d.%m}–{end - timedelta(days=1):%d.%m}"
    result = {"месяц": month, "дни": period, "учёт бота, $": str(round(bot, 2)),
              "счёт Anthropic, $": str(round(bill, 2)), "дней в счёте": days}
    if days == 0:
        return {**result, "skipped": "счёта за месяц нет (не задан ключ или не загружен)"}
    if bot == 0 and bill == 0:
        return {**result, "skipped": "расхода не было"}
    deviation = abs(bot - bill) / max(bill, bot)
    result["расхождение, %"] = str(round(deviation * 100, 1))
    if deviation <= Decimal(str(settings.anthropic_reconcile_tolerance)):
        return {**result, "ok": True}
    from app.messages import client as client_messages
    from app.messages import manager as manager_messages

    if not force and not await client_messages.claim_once(f"anthropic_reconcile:{month}", "anthropic_reconcile", 0):
        return {**result, "ok": False, "skipped": "уже сообщали"}
    side = "счёт больше учёта бота" if bill > bot else "учёт бота больше счёта"
    await manager_messages.notify(
        manager_messages.ANTHROPIC_COSTS,
        f"⚠️ <b>Claude API за {period}: учёт бота и счёт Anthropic расходятся на {result['расхождение, %']} %</b>\n"
        f"Учёт бота (llm_usage): ${round(bot, 2)} · счёт Anthropic: ${round(bill, 2)} — {side}.\n"
        "Счёт — по всей организации: проверьте, кто ещё пользуется ключом, и цены в llm_prices.py.",
        chat_id=settings.telegram_ops_chat_id or None,
    )
    return {**result, "ok": False, "sent": True}
