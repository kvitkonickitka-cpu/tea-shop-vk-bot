"""Тревоги Ops о выгрузке Т-Банка (cashflow) — тиком бота, раз в 5 минут.

Cashflow сам о сбоях сказать не может: Telegram у него не настроен, а с ВМ
Timeweb напрямую и не открывается — только через прокси на Aeza, который
знает бот. Поэтому бот читает то, что cashflow и так оставляет в общей
базе, и пишет в чат Ops сам:

- `finance.sync_state` — по каждому счёту итог последней выгрузки и текст
  ошибки (`last_status`, `last_error`), ошибка проверки холдов;
- `heartbeats` — отметки systemd: `cashflow`, `cashflow-backup` при успехе
  (`ops/vm/ops-heartbeat@.service`) и `сбой:cashflow`, `сбой:cashflow-backup`
  при падении юнита (`ops/vm/ops-failure@.service`). Сбой юнита ловит то, до
  чего выгрузка не дошла: справочники не загрузились, классификация упала,
  скрипт копии не запустился (17.10.2026 — 203/EXEC, копия молча не сделалась).

Что считаем поломкой:
- выгрузка по счёту закончилась ошибкой — вид ошибки (`auth` — токен или
  IP, `timeout`, `http_5xx`…) и что делать;
- удачной выгрузки нет дольше `SYNC_STALE` — таймер не сработал или задача
  умерла раньше, чем записала итог. Без этого истёкший токен выглядел бы как
  «выписка старая, но всё хорошо»;
- юнит cashflow или копии упал позже последнего успеха;
- копии нет дольше `BACKUP_STALE`;
- холды не проверились — предупреждение, выгрузка при этом идёт.

Текст ошибки банка в чат не уходит: в нём бывает ответ банка целиком, а
это номера счетов. Только вид ошибки и HTTP-код. Каждая проблема — одно
сообщение, повтор не чаще `REPEAT`, починилось — 🟢. Ключ проблемы — без
вида ошибки: сменился вид (`timeout` → `auth`) — поломка та же, и 🟢 между
ними был бы неправдой. Что уже объявлено,
помнят отметки `heartbeats` с именем `ops-cashflow:<проблема>`.

Схемы `finance` может не быть (тесты, контур без cashflow) — тогда молчим.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.core import worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.core.heartbeat import Heartbeat

logger = logging.getLogger(__name__)

# Выгрузка — в 07:00 и 21:00 по Москве со сдвигом до 5 минут: самый длинный
# законный промежуток — 14 часов. Час сверху — на долгий прогон и тик.
SYNC_STALE = timedelta(hours=15)
# Копия — раз в сутки в 06:30.
BACKUP_STALE = timedelta(hours=26)
# Задача идёт дважды в сутки: чаще напоминать о той же поломке — шум.
REPEAT = timedelta(hours=12)

NOTICE_PREFIX = "ops-cashflow:"
FAILURE_PREFIX = "сбой:"
UNITS = ("cashflow", "cashflow-backup")
DOCTOR = (
    "sudo -u cashflow env CASHFLOW_ENV_FILE=/etc/cashflow/.env "
    "/opt/cashflow/.venv/bin/python -m cashflow doctor"
)

_KIND_TEXT = {
    "auth": "банк отказал в доступе",
    "rate_limit": "банк ограничил частоту запросов",
    "http_5xx": "банк не ответил — ошибка на его стороне",
    "timeout": "банк не ответил за все попытки",
    "network": "нет связи с банком",
    "tls": "не прошла проверка сертификата банка",
    "http_4xx": "банк отклонил запрос",
    "parse": "не разобрали ответ банка",
    "other": "неожиданная ошибка",
}
_KIND_HINT = {
    "auth": (
        "Токен T-API отозван или истёк (живёт 90 дней с последнего запроса), либо запрос ушёл не с "
        "IP ВМ 201.24.120.144, к которому он привязан. Новый токен — в /etc/cashflow/.env (TBANK_TOKEN)."
    ),
    "rate_limit": "Обычно проходит само к следующему запуску.",
    "http_5xx": "Обычно проходит само к следующему запуску.",
    "timeout": "Если повторится — проверить сеть ВМ и статус Т-Бизнеса.",
    "network": "Проверить сеть ВМ: curl -sI https://business.tbank.ru.",
    "tls": "Сертификаты Минцифры в REQUESTS_CA_BUNDLE — doctor скажет, чего нет.",
    "http_4xx": "Банк поменял запрос или параметры — лог /var/log/cashflow/cashflow.log.",
    "parse": "Банк поменял формат выписки — лог /var/log/cashflow/cashflow.log, правка в tbank.py.",
    "other": "Лог /var/log/cashflow/cashflow.log.",
}


@dataclass
class Problem:
    key: str
    text: str


def error_kind(message: str | None) -> tuple[str, int | None]:
    """Вид ошибки и HTTP-код по тексту, который cashflow кладёт в sync_state.

    Тексты — из `cashflow.tbank.TBankError`: «Банк отказал в доступе (HTTP 403)»,
    «HTTP 502, X-Request-Id=…», «Не удалось получить выписку за 5 попыток: …».
    """
    message = message or ""
    found = re.search(r"HTTP (\d{3})", message)
    code = int(found.group(1)) if found else None
    lowered = message.lower()
    if code in (401, 403) or "отказал в доступе" in lowered:
        return "auth", code
    if code == 429:
        return "rate_limit", code
    if code is not None and code >= 500:
        return "http_5xx", code
    if "ssl" in lowered or "certificate" in lowered or "сертификат" in lowered:
        return "tls", code
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout", code
    if any(word in lowered for word in ("connect", "name or service", "network", "resolve")):
        return "network", code
    if code is not None and code >= 400:
        return "http_4xx", code
    if "не разобрал" in lowered or "в операции нет" in lowered:
        return "parse", code
    if "попыток" in lowered:
        # httpx-таймаут печатается пустой строкой: «за 5 попыток: » без хвоста.
        return "timeout", code
    return "other", code


def _msk(moment: datetime | None) -> str:
    return worktime.to_msk(moment).strftime("%d.%m %H:%M") + " МСК" if moment else "ни разу"


def _kind_label(kind: str, code: int | None) -> str:
    return f"{_KIND_TEXT[kind]} (<code>{kind}</code>{f', HTTP {code}' if code else ''})"


def evaluate(state: dict, now: datetime) -> list[Problem]:
    """Чистая логика: что из состояния cashflow — поломка. Без базы и отправки."""
    accounts = state.get("accounts") or []
    beats = state.get("beats") or {}
    if not accounts and not any(name in beats for name in UNITS):
        return []  # cashflow на этом контуре нет

    problems: list[Problem] = []
    for account in accounts:
        last4 = account["last4"]
        success = account.get("last_success_at")
        if account.get("last_status") not in (None, "ok"):
            kind, code = error_kind(account.get("last_error"))
            problems.append(Problem(
                f"sync:{last4}",
                f"🔴 <b>Выгрузка Т-Банка не прошла</b>: {_kind_label(kind, code)}, счёт …{last4}.\n"
                f"Последняя удачная — {_msk(success)}.\n{_KIND_HINT[kind]}\n"
                f"Проверка на ВМ: <code>{DOCTOR}</code>",
            ))
        elif success is None or now - success > SYNC_STALE:
            problems.append(Problem(
                f"sync-stale:{last4}",
                f"⚠️ <b>Выгрузки Т-Банка нет</b> с {_msk(success)}, счёт …{last4} — а идёт она в 07:00 и "
                "21:00. Таймер не сработал или задача упала раньше, чем записала итог.\n"
                "На ВМ: <code>systemctl list-timers cashflow.timer</code>, "
                "<code>journalctl -u cashflow.service -n 30</code>",
            ))
        if account.get("pending_error"):
            kind, code = error_kind(account["pending_error"])
            problems.append(Problem(
                f"holds:{last4}",
                f"⚠️ <b>Холды Т-Банка не проверились</b>: {_kind_label(kind, code)}, счёт …{last4}. "
                "Выписка при этом выгружается, в дашборде холды помечены «проверить не удалось».",
            ))

    for unit in UNITS:
        failed, ok = beats.get(FAILURE_PREFIX + unit), beats.get(unit)
        if failed and (ok is None or failed > ok):
            what = "Выгрузка Т-Банка" if unit == "cashflow" else "Резервная копия схемы finance"
            log = "cashflow.log" if unit == "cashflow" else "backup.log"
            problems.append(Problem(
                f"unit:{unit}",
                f"🔴 <b>{what} упала</b> в {_msk(failed)} (<code>{unit}.service</code>), "
                f"последний успех — {_msk(ok)}.\nНа ВМ: <code>journalctl -u {unit}.service -n 30</code>, "
                f"лог /var/log/cashflow/{log}",
            ))
    backup = beats.get("cashflow-backup")
    if backup is not None and now - backup > BACKUP_STALE and not any(p.key == "unit:cashflow-backup" for p in problems):
        problems.append(Problem(
            "backup-stale",
            f"⚠️ <b>Резервной копии схемы finance нет</b> с {_msk(backup)} — а делается она каждый день в "
            "06:30.\nНа ВМ: <code>systemctl list-timers cashflow-backup.timer</code>, "
            "<code>journalctl -u cashflow-backup.service -n 30</code>",
        ))
    return problems


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _sqlstate(error: DBAPIError) -> str | None:
    # Адаптер asyncpg у SQLAlchemy кладёт код то в pgcode, то в исходное
    # исключение asyncpg — смотрим оба места.
    orig = getattr(error, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return code
    return None


async def read_state() -> dict:
    """Итоги выгрузки по счетам и отметки systemd. Схемы finance нет — счетов нет."""
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(select(Heartbeat))).scalars().all()
    beats = {row.name: _utc(row.last_run_at) for row in rows}

    accounts: list[dict] = []
    try:
        async with factory() as session:
            result = await session.execute(text(
                """
                select right(account_number, 4) as last4, last_run_at, last_success_at,
                       last_status, last_error, pending_checked_at, pending_error, synced_through
                from finance.sync_state order by account_number
                """
            ))
            accounts = [dict(row._mapping) for row in result]
    except DBAPIError as error:
        # undefined_table / invalid_schema_name: cashflow на этом контуре не стоит.
        if _sqlstate(error) not in ("42P01", "3F000"):
            raise
    for account in accounts:
        for field in ("last_run_at", "last_success_at", "pending_checked_at"):
            account[field] = _utc(account[field])
    return {"accounts": accounts, "beats": beats}


async def _mark(name: str, at: datetime) -> None:
    """Отметка «объявлено» со временем проверки, а не часов: тик и тесты — одно время."""
    statement = insert(Heartbeat).values(name=name, last_run_at=at, runs=1)
    async with get_session_factory()() as session:
        await session.execute(statement.on_conflict_do_update(
            index_elements=[Heartbeat.name],
            set_={"last_run_at": statement.excluded.last_run_at, "runs": Heartbeat.runs + 1},
        ))
        await session.commit()


async def _forget(names: list[str]) -> None:
    async with get_session_factory()() as session:
        await session.execute(delete(Heartbeat).where(Heartbeat.name.in_(names)))
        await session.commit()


async def check(now: datetime | None = None) -> dict:
    """Тик: объявить новые поломки, напомнить о старых раз в REPEAT, сказать 🟢."""
    if not settings.telegram_ops_chat_id:
        return {"skipped": "нет чата Ops"}
    from app.modules.ops import alerts

    now = now or datetime.now(timezone.utc)
    state = await read_state()
    problems = evaluate(state, now)
    current = {problem.key for problem in problems}
    announced = {
        name[len(NOTICE_PREFIX):]: moment
        for name, moment in state["beats"].items() if name.startswith(NOTICE_PREFIX)
    }

    sent = []
    for problem in problems:
        last = announced.get(problem.key)
        if last is not None and now - last < REPEAT:
            continue
        await alerts._send(problem.text)
        await _mark(NOTICE_PREFIX + problem.key, now)
        sent.append(problem.key)

    resolved = sorted(key for key in announced if key not in current)
    if resolved:
        lines = []
        for key in resolved:
            kind, _, rest = key.partition(":")
            if kind in ("sync", "sync-stale"):
                lines.append(f"выгрузка по счёту …{rest}")
            elif kind == "holds":
                lines.append(f"холды по счёту …{rest}")
            elif key in ("unit:cashflow-backup", "backup-stale"):
                lines.append("резервная копия finance")
            else:
                lines.append("задача cashflow")
        await alerts._send("🟢 <b>Cashflow снова в порядке</b>: " + ", ".join(dict.fromkeys(lines)) + ".")
        await _forget([NOTICE_PREFIX + key for key in resolved])
    return {"problems": sorted(current), "sent": sent, "resolved": resolved}
