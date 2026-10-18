"""Чтение Google Таблиц через Sheets API v4 сервисным аккаунтом — только чтение.

Зачем: опубликованная CSV-ссылка открыта всему интернету. С сервисным
аккаунтом таблицу можно закрыть («Доступ ограничен», публикация снята) и
расшарить только на его почту с ролью «Читатель».

Три правила модуля:

- **scope строго `spreadsheets.readonly`**: даже с ключом в руках этим
  кодом в таблицу не записать;
- **ключ не попадает ни в лог, ни в текст ошибки.** Ключ — переменная
  окружения `GOOGLE_SA_JSON` (JSON целиком) или `GOOGLE_SA_JSON_B64` (тот же
  JSON в base64 — удобнее для `.env`-файла: в одну строку и без кавычек). В
  ошибках — только почта сервисного аккаунта (она не секрет) и ответ Google;
- **ошибка доступа — громкая.** 403/404/неверная вкладка — `SheetsError` с
  объяснением, что чинить; временное (429, 5xx, таймаут) повторяется с
  паузой 1, 2, 4 с и, если не прошло, помечается `transient`. Тихого отката
  на публичную ссылку здесь нет и быть не должно: иначе сломанный доступ
  никто бы не заметил.

Токен живёт час и обновляется сам, когда истёк; запрос токена идёт в Google
синхронно (так устроен google-auth), поэтому — в отдельном потоке.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import threading

import httpx
from google.auth import exceptions as google_exceptions
from google.auth import transport as google_transport
from google.oauth2 import service_account

from app.core.config import settings

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_API = "https://sheets.googleapis.com/v4/spreadsheets"
_TIMEOUT_SECONDS = 10
# Повторы временных ошибок: паузы 1, 2, 4 с — всего до четырёх попыток,
# укладывается в тик расписания.
_RETRY_DELAYS = (1.0, 2.0, 4.0)
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class SheetsError(RuntimeError):
    """Таблицу не прочитали. Текст — для человека, без ключа.

    `transient` — временная беда Google (429, 5xx, сеть): следующий тик
    попробует снова. Остальное само не пройдёт — чинить доступ или настройки.
    """

    def __init__(self, message: str, *, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


def a1_range(tab: str, cells: str = "") -> str:
    """Диапазон в A1-нотации: имя вкладки всегда в кавычках.

    Кавычки нужны для пробелов, кириллицы и знаков в имени («Приходы и
    инвентаризация»); апостроф внутри имени удваивается.
    """
    quoted = "'" + tab.replace("'", "''") + "'"
    return f"{quoted}!{cells}" if cells else quoted


def _service_account_info() -> dict:
    raw = (settings.google_sa_json or "").strip()
    encoded = (settings.google_sa_json_b64 or "").strip()
    if not raw and encoded:
        try:
            raw = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            # Без `from error`: в тексте исключения может оказаться кусок ключа.
            raise SheetsError("GOOGLE_SA_JSON_B64 — не base64 от JSON-ключа") from None
    if not raw:
        raise SheetsError(
            "ключ сервисного аккаунта не задан: нужна переменная GOOGLE_SA_JSON "
            "(JSON целиком) или GOOGLE_SA_JSON_B64"
        )
    try:
        info = json.loads(raw)
    except ValueError:
        raise SheetsError("ключ сервисного аккаунта не разбирается как JSON") from None
    if not isinstance(info, dict) or info.get("type") != "service_account" \
            or not info.get("client_email") or not info.get("private_key"):
        raise SheetsError(
            "в ключе нет client_email или private_key — это не JSON-ключ сервисного аккаунта"
        )
    return info


class _Response(google_transport.Response):
    def __init__(self, response: httpx.Response):
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self):
        return self._response.headers

    @property
    def data(self) -> bytes:
        return self._response.content


class _Request(google_transport.Request):
    """Транспорт google-auth поверх httpx: своей зависимости `requests` не тащим."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        try:
            with httpx.Client(timeout=timeout or _TIMEOUT_SECONDS, transport=self._transport) as client:
                return _Response(client.request(method, url, content=body, headers=headers))
        except httpx.HTTPError as error:
            raise google_exceptions.TransportError(type(error).__name__) from None


class SheetsClient:
    """Клиент Sheets API. `transport` и `sleep` подменяются в тестах."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token_transport: httpx.BaseTransport | None = None,
        sleep=asyncio.sleep,
    ):
        self._transport = transport
        self._token_transport = token_transport
        self._sleep = sleep
        self._credentials: service_account.Credentials | None = None
        self._lock = threading.Lock()

    @property
    def account(self) -> str:
        """Почта сервисного аккаунта — для текстов ошибок, не секрет."""
        return self._credentials.service_account_email if self._credentials else ""

    def _refresh_sync(self) -> str:
        with self._lock:
            if self._credentials is None:
                info = _service_account_info()
                try:
                    self._credentials = service_account.Credentials.from_service_account_info(
                        info, scopes=[SCOPE]
                    )
                except (ValueError, KeyError):
                    raise SheetsError(
                        "закрытый ключ сервисного аккаунта не читается — ключ повреждён"
                    ) from None
            if not self._credentials.valid:
                try:
                    self._credentials.refresh(_Request(self._token_transport))
                except google_exceptions.TransportError as error:
                    raise SheetsError(
                        f"Google не ответил на запрос токена: {error}", transient=True
                    ) from None
                except google_exceptions.RefreshError as error:
                    # Текст от Google («invalid_grant: …»), ключа в нём нет.
                    reason = str(error.args[0] if error.args else error)[:200]
                    raise SheetsError(
                        f"Google не выдал токен сервисному аккаунту {self.account}: {reason}. "
                        "Ключ отозван или удалён — нужен новый ключ"
                    ) from None
            return self._credentials.token

    async def _token(self) -> str:
        return await asyncio.to_thread(self._refresh_sync)

    def _forget_token(self) -> None:
        with self._lock:
            if self._credentials is not None:
                self._credentials.token = None

    def _explain(self, status: int, body: str, spreadsheet_id: str, ranges: list[str]) -> SheetsError:
        try:
            error = (json.loads(body) or {}).get("error") or {}
        except ValueError:
            error = {}
        message = str(error.get("message") or "")[:200]
        if status == 403:
            if "SERVICE_DISABLED" in body or "accessNotConfigured" in body or "has not been used" in message:
                return SheetsError(
                    "Sheets API не включён в проекте Google Cloud сервисного аккаунта "
                    f"(APIs & Services → Library → Google Sheets API → Enable). Google: {message}",
                    status=status,
                )
            return SheetsError(
                f"нет доступа к таблице {spreadsheet_id}: она не расшарена на "
                f"{self.account or 'сервисный аккаунт'} («Поделиться» → его почта, роль «Читатель»). "
                f"Google: {message}",
                status=status,
            )
        if status == 404:
            return SheetsError(
                f"таблица {spreadsheet_id} не найдена: неверный id или таблица удалена. Google: {message}",
                status=status,
            )
        if status == 400 and "parse range" in message.lower():
            return SheetsError(
                f"нет такой вкладки или диапазона: {', '.join(ranges)} — вкладку "
                f"переименовали? Google: {message}",
                status=status,
            )
        return SheetsError(f"Sheets API ответил {status}: {message or body[:200]}", status=status)

    async def batch_get(
        self,
        spreadsheet_id: str,
        ranges: list[str],
        *,
        render: str = "FORMATTED_VALUE",
    ) -> list[list[list[str]]]:
        """Значения диапазонов: на каждый диапазон — строки, в строке — ячейки-строки.

        `FORMATTED_VALUE` — как ячейки видны в таблице: «1 990», «1,5», та же
        строка, что в опубликованном CSV. Пустые хвостовые ячейки и строки
        Google обрезает — выравнивает вызывающий (`pad_rows`).
        """
        params = [("ranges", r) for r in ranges] + [
            ("valueRenderOption", render),
            ("dateTimeRenderOption", "FORMATTED_STRING"),
            ("majorDimension", "ROWS"),
        ]
        url = f"{_API}/{spreadsheet_id}/values:batchGet"
        token_retried = False
        attempt = 0
        while True:
            token = await self._token()
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport) as client:
                    response = await client.get(
                        url, params=params, headers={"Authorization": f"Bearer {token}"}
                    )
            except httpx.HTTPError as error:
                status, problem = None, type(error).__name__
            else:
                status, problem = response.status_code, ""
                if status == 200:
                    data = response.json()
                    return [
                        [[str(cell) for cell in row] for row in (value_range.get("values") or [])]
                        for value_range in data.get("valueRanges") or []
                    ]
                if status == 401 and not token_retried:
                    # Токен отозвали раньше срока — один раз берём новый.
                    token_retried = True
                    self._forget_token()
                    continue
                if status not in _RETRY_STATUSES:
                    raise self._explain(status, response.text, spreadsheet_id, ranges)
                problem = f"HTTP {status}"

            if attempt >= len(_RETRY_DELAYS):
                raise SheetsError(
                    f"Sheets API недоступен ({problem}) после {attempt + 1} попыток",
                    status=status, transient=True,
                )
            delay = _RETRY_DELAYS[attempt]
            attempt += 1
            logger.warning("Sheets API: %s, повтор через %.0f с", problem, delay)
            await self._sleep(delay)


def pad_rows(rows: list[list[str]]) -> list[list[str]]:
    """Выровнять строки по самой широкой: Google обрезает пустые хвосты.

    Опубликованный CSV отдаёт каждую строку на всю ширину таблицы; разбор
    каталога ищет столбцы по номеру из заголовка, и короткая строка с
    пустыми последними ячейками не должна от этого отличаться.
    """
    width = max((len(row) for row in rows), default=0)
    return [list(row) + [""] * (width - len(row)) for row in rows]


_client: SheetsClient | None = None


def client() -> SheetsClient:
    """Общий клиент на процесс: токен живёт час, незачем брать его каждый тик."""
    global _client
    if _client is None:
        _client = SheetsClient()
    return _client
