"""Загрузка выписки в finance.raw_operations. Повторный запуск не создаёт дублей."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from .config import Config
from .engine import counterparty_kind
from .tbank import Balance, Operation, TBankClient

log = logging.getLogger(__name__)

UPSERT_OPERATION = """
INSERT INTO finance.raw_operations (
    operation_id, account_number, operation_date, operation_ts, direction, amount,
    counterparty_name, counterparty_inn, counterparty_account, purpose,
    tbank_category, operation_status, raw_json
)
VALUES (%(operation_id)s, %(account_number)s, %(operation_date)s, %(operation_ts)s,
        %(direction)s, %(amount)s, %(counterparty_name)s, %(counterparty_inn)s,
        %(counterparty_account)s, %(purpose)s, %(tbank_category)s,
        %(operation_status)s, %(raw_json)s)
ON CONFLICT (operation_id) DO UPDATE SET
    account_number       = EXCLUDED.account_number,
    operation_date       = EXCLUDED.operation_date,
    operation_ts         = EXCLUDED.operation_ts,
    direction            = EXCLUDED.direction,
    amount               = EXCLUDED.amount,
    counterparty_name    = EXCLUDED.counterparty_name,
    counterparty_inn     = EXCLUDED.counterparty_inn,
    counterparty_account = EXCLUDED.counterparty_account,
    purpose              = EXCLUDED.purpose,
    tbank_category       = EXCLUDED.tbank_category,
    operation_status     = EXCLUDED.operation_status,
    raw_json             = EXCLUDED.raw_json,
    updated_at           = now()
RETURNING (xmax = 0) AS inserted
"""

UPSERT_BALANCE = """
INSERT INTO finance.balances (account_number, as_of_date, balance_kind, amount, raw_json)
VALUES (%(account_number)s, %(as_of_date)s, %(kind)s, %(amount)s, %(raw_json)s)
ON CONFLICT (account_number, as_of_date, balance_kind) DO UPDATE SET
    amount     = EXCLUDED.amount,
    raw_json   = EXCLUDED.raw_json,
    fetched_at = now()
"""


@dataclass
class SyncResult:
    account_number: str
    period_from: date
    period_to: date
    inserted: int = 0
    updated: int = 0
    balances: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated


def save_operations(conn: psycopg.Connection, operations: list[Operation]) -> tuple[int, int]:
    inserted = updated = 0
    with conn.cursor() as cur:
        for op in operations:
            cur.execute(
                UPSERT_OPERATION,
                {
                    "operation_id": op.operation_id,
                    "account_number": op.account_number,
                    "operation_date": op.operation_date,
                    "operation_ts": op.operation_ts,
                    "direction": op.direction,
                    "amount": op.amount,
                    "counterparty_name": op.counterparty_name,
                    "counterparty_inn": op.counterparty_inn,
                    "counterparty_account": op.counterparty_account,
                    "purpose": op.purpose,
                    "tbank_category": op.tbank_category,
                    "operation_status": op.operation_status,
                    "raw_json": json.dumps(op.raw, ensure_ascii=False),
                },
            )
            row = cur.fetchone()
            if row and row[0]:
                inserted += 1
            else:
                updated += 1
    return inserted, updated


def save_balances(conn: psycopg.Connection, balances: list[Balance]) -> int:
    with conn.cursor() as cur:
        for balance in balances:
            cur.execute(
                UPSERT_BALANCE,
                {
                    "account_number": balance.account_number,
                    "as_of_date": balance.as_of_date,
                    "kind": balance.kind,
                    "amount": balance.amount,
                    "raw_json": json.dumps(balance.raw, ensure_ascii=False),
                },
            )
    return len(balances)


def _period_start(conn: psycopg.Connection, cfg: Config, account: str, full: bool) -> date:
    if full:
        return cfg.first_day
    with conn.cursor() as cur:
        cur.execute(
            "SELECT synced_through FROM finance.sync_state WHERE account_number = %s", (account,)
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return cfg.first_day
    # Перекрытие: банк может дозаписать операции задним числом.
    return max(cfg.first_day, row[0] - timedelta(days=cfg.sync_overlap_days))


def _record_state(
    conn: psycopg.Connection, account: str, through: date, status: str, error: str | None, seen: int
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finance.sync_state (
                account_number, synced_through, last_run_at, last_success_at,
                last_status, last_error, operations_seen)
            VALUES (%s, %s, now(), CASE WHEN %s = 'ok' THEN now() END, %s, %s, %s)
            ON CONFLICT (account_number) DO UPDATE SET
                synced_through  = CASE WHEN %s = 'ok'
                                       THEN GREATEST(finance.sync_state.synced_through, EXCLUDED.synced_through)
                                       ELSE finance.sync_state.synced_through END,
                last_run_at     = now(),
                last_success_at = CASE WHEN %s = 'ok' THEN now()
                                       ELSE finance.sync_state.last_success_at END,
                last_status     = EXCLUDED.last_status,
                last_error      = EXCLUDED.last_error,
                operations_seen = finance.sync_state.operations_seen + EXCLUDED.operations_seen
            """,
            (account, through, status, status, error, seen, status, status),
        )


def sync_account(
    conn: psycopg.Connection,
    client: TBankClient,
    cfg: Config,
    account: str,
    period_from: date,
    period_to: date,
) -> SyncResult:
    result = SyncResult(account_number=account, period_from=period_from, period_to=period_to)
    try:
        for operations, balances in client.statement(account, period_from, period_to):
            inserted, updated = save_operations(conn, operations)
            result.inserted += inserted
            result.updated += updated
            result.balances += save_balances(conn, balances)
            conn.commit()
    except Exception as exc:
        conn.rollback()
        _record_state(conn, account, period_to, "error", str(exc)[:1000], 0)
        conn.commit()
        raise

    _record_state(conn, account, period_to, "ok", None, result.total)
    conn.commit()
    log.info(
        "Счёт %s за %s…%s: новых %s, обновлено %s, остатков %s",
        account[-4:], period_from, period_to, result.inserted, result.updated, result.balances,
    )
    return result


UPSERT_PENDING = """
INSERT INTO finance.pending_operations (
    operation_id, account_number, operation_date, operation_ts, direction, amount,
    counterparty_name, counterparty_inn, counterparty_kind, purpose, operation_status, raw_json
)
VALUES (%(operation_id)s, %(account_number)s, %(operation_date)s, %(operation_ts)s,
        %(direction)s, %(amount)s, %(counterparty_name)s, %(counterparty_inn)s,
        %(counterparty_kind)s, %(purpose)s, %(operation_status)s, %(raw_json)s)
ON CONFLICT (operation_id) DO UPDATE SET
    account_number    = EXCLUDED.account_number,
    operation_date    = EXCLUDED.operation_date,
    operation_ts      = EXCLUDED.operation_ts,
    direction         = EXCLUDED.direction,
    amount            = EXCLUDED.amount,
    counterparty_name = EXCLUDED.counterparty_name,
    counterparty_inn  = EXCLUDED.counterparty_inn,
    counterparty_kind = EXCLUDED.counterparty_kind,
    purpose           = EXCLUDED.purpose,
    operation_status  = EXCLUDED.operation_status,
    raw_json          = EXCLUDED.raw_json,
    last_seen_at      = now()
"""


def sync_pending(
    conn: psycopg.Connection, client: TBankClient, cfg: Config, account: str, today: date
) -> int | None:
    """Снимок холдов по счёту: записать, что вернул банк, остальное убрать.

    Всё — одной транзакцией: упал запрос на середине — снимок остаётся
    прежним, а в sync_state отмечена ошибка (витрина покажет check_failed).
    Основную выгрузку сбой здесь не останавливает: холды — справка, а не учёт.
    Возвращает число холдов или None при сбое.
    """
    since = today - timedelta(days=cfg.pending_lookback_days)
    try:
        operations: list[Operation] = []
        for page in client.authorizations(account, since, today):
            operations.extend(page)
        with conn.cursor() as cur:
            for op in operations:
                cur.execute(UPSERT_PENDING, {
                    "operation_id": op.operation_id,
                    "account_number": op.account_number,
                    "operation_date": op.operation_date,
                    "operation_ts": op.operation_ts,
                    "direction": op.direction,
                    "amount": op.amount,
                    "counterparty_name": op.counterparty_name,
                    "counterparty_inn": op.counterparty_inn,
                    "counterparty_kind": counterparty_kind(op.counterparty_inn),
                    "purpose": op.purpose,
                    "operation_status": op.operation_status,
                    "raw_json": json.dumps(op.raw, ensure_ascii=False),
                })
            # Чего в свежем снимке нет — провелось или отменилось.
            cur.execute(
                "DELETE FROM finance.pending_operations "
                "WHERE account_number = %s AND NOT (operation_id = ANY(%s::text[]))",
                (account, [op.operation_id for op in operations]),
            )
            cur.execute(
                "UPDATE finance.sync_state SET pending_checked_at = now(), pending_error = NULL "
                "WHERE account_number = %s",
                (account,),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — холды не должны ронять выгрузку
        conn.rollback()
        log.warning("Счёт %s: холды не получены — %s", account[-4:], str(exc)[:300])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE finance.sync_state SET pending_error = %s WHERE account_number = %s",
                (str(exc)[:1000], account),
            )
        conn.commit()
        return None
    log.info("Счёт %s: в холде %s операций", account[-4:], len(operations))
    return len(operations)


def run_sync(
    conn: psycopg.Connection,
    client: TBankClient,
    cfg: Config,
    full: bool = False,
    period_from: date | None = None,
    period_to: date | None = None,
) -> list[SyncResult]:
    to_day = period_to or date.today()
    results = []
    for account in cfg.accounts:
        from_day = period_from or _period_start(conn, cfg, account, full)
        results.append(sync_account(conn, client, cfg, account, from_day, to_day))
        # После основной выгрузки: sync_state по счёту уже есть, и отметка
        # о проверке холдов ляжет в ту же строку.
        sync_pending(conn, client, cfg, account, to_day)
    return results
