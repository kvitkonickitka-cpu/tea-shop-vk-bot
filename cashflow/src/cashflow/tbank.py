"""Клиент выписки Т-Бизнеса: GET /api/v1/statement.

Имена полей в ответе разбираются терпимо: банк может называть их по-разному
в песочнице и в проде. Если структура ответа разойдётся с документацией T-API,
приоритет у документации — правьте _pick_* функции здесь.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

MOSCOW = ZoneInfo("Europe/Moscow")
PAGE_LIMIT = 5000
MAX_ATTEMPTS = 5
# Операции в других статусах (холд, отмена) в ленту не попадают.
WANTED_STATUS = "transaction"


class TBankError(RuntimeError):
    pass


@dataclass(frozen=True)
class Operation:
    operation_id: str
    account_number: str
    operation_date: date
    operation_ts: datetime | None
    direction: str
    amount: Decimal
    counterparty_name: str | None
    counterparty_inn: str | None
    counterparty_account: str | None
    purpose: str | None
    tbank_category: str | None
    operation_status: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class Balance:
    account_number: str
    as_of_date: date
    kind: str
    amount: Decimal
    raw: dict[str, Any]


def _first(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        raise TBankError("В операции нет суммы")
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise TBankError(f"Не разобрал сумму: {value!r}") from exc


def _to_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _operation_date(payload: dict[str, Any]) -> tuple[date, datetime | None]:
    """Дата, по которой операция попадает в отчётность.

    Берём дату списания/зачисления по счёту, а не дату документа, и переводим
    в московское время: операция ночью по UTC не должна уезжать на сутки назад.
    """
    raw_value = _first(
        payload, "chargeDate", "operationDate", "drawDate", "date", "authorizationDate", "createdAt"
    )
    moment = _to_datetime(raw_value)
    if moment is None:
        raise TBankError(f"Не разобрал дату операции: {raw_value!r}")
    local = moment.astimezone(MOSCOW)
    return local.date(), moment


def _direction(payload: dict[str, Any], account_number: str) -> str:
    marker = str(_first(payload, "typeOfOperation", "operationType", "direction") or "").lower()
    if marker in {"credit", "in", "income", "incoming"}:
        return "in"
    if marker in {"debit", "out", "outcome", "outgoing", "expense"}:
        return "out"
    payer_account = str(_first(payload, "payerAccount", "payerAccountNumber") or "")
    if payer_account and payer_account == account_number:
        return "out"
    receiver_account = str(_first(payload, "recipientAccount", "receiverAccount") or "")
    if receiver_account and receiver_account == account_number:
        return "in"
    raise TBankError(
        f"Не понял направление операции {_first(payload, 'operationId', 'id')!r}. "
        "Проверьте поля typeOfOperation / payerAccount в ответе T-API."
    )


def parse_operation(payload: dict[str, Any], account_number: str) -> Operation:
    operation_id = _first(payload, "operationId", "id", "uuid", "operationUuid")
    if not operation_id:
        raise TBankError("В операции нет идентификатора — дедупликация невозможна")

    direction = _direction(payload, account_number)
    operation_date, operation_ts = _operation_date(payload)

    if direction == "in":
        name = _first(payload, "payerName", "payer", "counterPartyName")
        inn = _first(payload, "payerInn", "counterPartyInn")
        account = _first(payload, "payerAccount", "payerAccountNumber")
    else:
        name = _first(payload, "recipientName", "receiverName", "counterPartyName")
        inn = _first(payload, "recipientInn", "receiverInn", "counterPartyInn")
        account = _first(payload, "recipientAccount", "receiverAccount")

    return Operation(
        operation_id=str(operation_id),
        account_number=account_number,
        operation_date=operation_date,
        operation_ts=operation_ts,
        direction=direction,
        amount=_to_decimal(_first(payload, "accountAmount", "amount", "operationAmount")),
        counterparty_name=str(name) if name else None,
        counterparty_inn=str(inn) if inn else None,
        counterparty_account=str(account) if account else None,
        purpose=_first(payload, "paymentPurpose", "purpose", "description"),
        tbank_category=_first(payload, "category", "operationCategory", "rubric"),
        operation_status=_first(payload, "operationStatus", "status"),
        raw=payload,
    )


def _extract_operations(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        for key in ("operations", "items", "data", "transactions", "result"):
            value = body.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_cursor(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("nextCursor", "next_cursor", "cursor"):
        value = body.get(key)
        if value:
            return str(value)
    paging = body.get("paging") or body.get("pagination")
    if isinstance(paging, dict):
        for key in ("nextCursor", "next_cursor", "cursor"):
            if paging.get(key):
                return str(paging[key])
    return None


def _extract_balances(body: Any, account_number: str, period_to: date) -> list[Balance]:
    if not isinstance(body, dict):
        return []
    source = body.get("balances") if isinstance(body.get("balances"), dict) else body
    result: list[Balance] = []
    mapping = {
        "opening": ("openingBalance", "balanceOpening", "startBalance", "openingAmount"),
        "closing": ("closingBalance", "balanceClosing", "endBalance", "closingAmount", "balance"),
    }
    for kind, names in mapping.items():
        value = _first(source, *names) if isinstance(source, dict) else None
        if value is None:
            continue
        if isinstance(value, dict):
            value = _first(value, "amount", "value", "sum")
        if value is None:
            continue
        try:
            amount = _to_decimal(value)
        except TBankError:
            continue
        result.append(
            Balance(
                account_number=account_number,
                as_of_date=period_to,
                kind=kind,
                amount=amount,
                raw=source if isinstance(source, dict) else {},
            )
        )
    return result


class TBankClient:
    def __init__(
        self,
        token: str,
        api_base: str,
        ca_bundle: str | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        verify: Any = ca_bundle if ca_bundle else True
        self._client = client or httpx.Client(timeout=timeout, verify=verify)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TBankClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self._api_base}{path}"
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            request_id = str(uuid.uuid4())
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "X-Request-Id": request_id,
            }
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("Сетевая ошибка при запросе выписки (попытка %s): %s", attempt, exc)
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code in (401, 403):
                    raise TBankError(
                        f"Банк отказал в доступе (HTTP {response.status_code}). Проверьте токен, "
                        "его скоуп «Счета и выписки» и что запрос идёт со статического IP ВМ. "
                        f"X-Request-Id={request_id}"
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = TBankError(
                        f"HTTP {response.status_code}, X-Request-Id={request_id}"
                    )
                    log.warning(
                        "Банк ответил %s (попытка %s из %s)", response.status_code, attempt, MAX_ATTEMPTS
                    )
                else:
                    raise TBankError(
                        f"Банк ответил HTTP {response.status_code}: {response.text[:300]} "
                        f"X-Request-Id={request_id}"
                    )

            if attempt < MAX_ATTEMPTS:
                # Экспоненциальная пауза: 2, 4, 8, 16 секунд.
                time.sleep(2**attempt)

        raise TBankError(f"Не удалось получить выписку за {MAX_ATTEMPTS} попыток: {last_error}")

    def statement(
        self, account_number: str, period_from: date, period_to: date
    ) -> Iterator[tuple[list[Operation], list[Balance]]]:
        """Отдаёт выписку постранично: (операции страницы, остатки первой страницы)."""
        cursor: str | None = None
        page = 0

        while True:
            params: dict[str, Any] = {
                "accountNumber": account_number,
                "from": f"{period_from.isoformat()}T00:00:00Z",
                "to": f"{period_to.isoformat()}T23:59:59Z",
                "limit": PAGE_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor
            else:
                params["withBalances"] = "true"

            body = self._get("/api/v1/statement", params)
            page += 1

            operations: list[Operation] = []
            for item in _extract_operations(body):
                status = str(_first(item, "operationStatus", "status") or "").lower()
                if status and status != WANTED_STATUS:
                    continue
                operations.append(parse_operation(item, account_number))

            balances = _extract_balances(body, account_number, period_to) if page == 1 else []
            log.info(
                "Счёт %s: страница %s, операций %s", account_number[-4:], page, len(operations)
            )
            yield operations, balances

            cursor = _extract_cursor(body)
            if not cursor:
                break
