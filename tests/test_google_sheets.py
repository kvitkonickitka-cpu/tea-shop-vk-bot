"""Чтение таблиц сервисным аккаунтом: без сети, Google подменён.

Ключ — одноразовый RSA, сгенерированный здесь же: настоящего ключа в тестах
нет и быть не должно.
"""

from __future__ import annotations

import base64
import json
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import google_sheets
from app.core.config import settings

ACCOUNT = "tea-bot-sheets-reader@test-project.iam.gserviceaccount.com"
SHEET_ID = "sheet-id-for-tests"


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


PEM = _pem()
# Кусок ключа, которого не должно быть ни в логе, ни в тексте ошибки.
KEY_FRAGMENT = PEM.splitlines()[5]


def sa_json(**overrides) -> str:
    info = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "0" * 40,
        "private_key": PEM,
        "client_email": ACCOUNT,
        "client_id": "1",
        "token_uri": "https://oauth2.test/token",
    }
    info.update(overrides)
    return json.dumps(info)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "google_sa_json", sa_json())
    monkeypatch.setattr(settings, "google_sa_json_b64", "")


class Google:
    """Подменённый Google: выдаёт токены и отвечает на values:batchGet."""

    def __init__(self, answers=None, token_status=200):
        self.answers = list(answers or [])
        self.token_status = token_status
        self.tokens_issued = 0
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def token(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "oauth2.test"
        # В запросе токена — подписанный JWT, а не сам ключ.
        assert KEY_FRAGMENT.encode() not in request.content
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={
                "error": "invalid_grant", "error_description": "Invalid JWT Signature.",
            })
        self.tokens_issued += 1
        return httpx.Response(200, json={"access_token": f"token-{self.tokens_issued}",
                                         "expires_in": 3600, "token_type": "Bearer"})

    def sheets(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else (200, {"valueRanges": []})
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def client(self) -> google_sheets.SheetsClient:
        return google_sheets.SheetsClient(
            transport=httpx.MockTransport(self.sheets),
            token_transport=httpx.MockTransport(self.token),
            sleep=self.sleep,
        )


def values(*rows) -> tuple[int, dict]:
    return 200, {"valueRanges": [{"range": "'Untitled'!A1:K4", "majorDimension": "ROWS",
                                  "values": [list(r) for r in rows]}]}


def google_error(status: int, message: str, reason: str = "") -> tuple[int, dict]:
    body = {"error": {"code": status, "message": message, "status": reason or "ERROR"}}
    if reason == "SERVICE_DISABLED":
        body["error"]["details"] = [{"reason": "SERVICE_DISABLED"}]
    return status, body


def test_a1_range_quotes_cyrillic_spaces_and_apostrophes():
    assert google_sheets.a1_range("Себестоимость", "A:F") == "'Себестоимость'!A:F"
    assert google_sheets.a1_range("Приходы и инвентаризация") == "'Приходы и инвентаризация'"
    assert google_sheets.a1_range("Справочники — как пользоваться", "A1:B20") == \
        "'Справочники — как пользоваться'!A1:B20"
    assert google_sheets.a1_range("Чай O'Brien", "A:A") == "'Чай O''Brien'!A:A"


async def test_batch_get_sends_ranges_and_a_readonly_token(key):
    google = Google([values(["Название", "Цена"], ["Те Гуань Инь", "1 990"])])
    client = google.client()
    ranges = [google_sheets.a1_range("Себестоимость", "A:F"), google_sheets.a1_range("Untitled")]
    result = await client.batch_get(SHEET_ID, ranges)

    assert result == [[["Название", "Цена"], ["Те Гуань Инь", "1 990"]]]
    [request] = google.requests
    assert request.url.path == f"/v4/spreadsheets/{SHEET_ID}/values:batchGet"
    query = parse_qs(urlsplit(str(request.url)).query)
    assert query["ranges"] == ["'Себестоимость'!A:F", "'Untitled'"]
    # Как ячейки видны в таблице — то же, что отдаёт опубликованный CSV.
    assert query["valueRenderOption"] == ["FORMATTED_VALUE"]
    assert request.headers["Authorization"] == "Bearer token-1"
    assert client.account == ACCOUNT

    # Токен живёт час: второй запрос его не перезапрашивает.
    google.answers.append(values(["a"]))
    await client.batch_get(SHEET_ID, ranges[:1])
    assert google.tokens_issued == 1


def test_scope_is_read_only():
    assert google_sheets.SCOPE == "https://www.googleapis.com/auth/spreadsheets.readonly"


async def test_key_may_come_in_base64(monkeypatch):
    monkeypatch.setattr(settings, "google_sa_json", "")
    monkeypatch.setattr(settings, "google_sa_json_b64", base64.b64encode(sa_json().encode()).decode())
    google = Google([values(["x"])])
    assert await google.client().batch_get(SHEET_ID, ["'Untitled'"]) == [[["x"]]]


def test_pad_rows_restores_trimmed_empty_cells():
    # Google обрезает пустые ячейки в конце строки.
    rows = [["Название", "Цена", "GTIN", "Описание"], ["Чай", "100"], ["Улун", "200", "", "вкусный"]]
    assert google_sheets.pad_rows(rows) == [
        ["Название", "Цена", "GTIN", "Описание"], ["Чай", "100", "", ""], ["Улун", "200", "", "вкусный"],
    ]
    assert google_sheets.pad_rows([]) == []


@pytest.mark.parametrize(
    "answer,expected",
    [
        (google_error(403, "The caller does not have permission", "PERMISSION_DENIED"),
         f"не расшарена на {ACCOUNT}"),
        (google_error(403, "Google Sheets API has not been used in project 1 before or it is disabled.",
                      "SERVICE_DISABLED"),
         "Sheets API не включён"),
        (google_error(404, "Requested entity was not found.", "NOT_FOUND"),
         f"таблица {SHEET_ID} не найдена"),
        (google_error(400, "Unable to parse range: 'Untitld'", "INVALID_ARGUMENT"),
         "нет такой вкладки или диапазона: 'Untitld'"),
    ],
)
async def test_access_errors_are_explained_and_not_retried(key, caplog, answer, expected):
    google = Google([answer])
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(google_sheets.SheetsError) as error:
            await google.client().batch_get(SHEET_ID, ["'Untitld'" if answer[0] == 400 else "'Untitled'"])
    assert expected in str(error.value)
    assert not error.value.transient and error.value.status == answer[0]
    assert google.sleeps == [] and len(google.requests) == 1
    assert KEY_FRAGMENT not in str(error.value) and KEY_FRAGMENT not in caplog.text


async def test_429_and_5xx_are_retried_with_backoff(key):
    google = Google([google_error(429, "Quota exceeded"), google_error(503, "Unavailable"),
                     values(["ok"])])
    assert await google.client().batch_get(SHEET_ID, ["'Untitled'"]) == [[["ok"]]]
    assert google.sleeps == [1.0, 2.0]


async def test_persistent_429_ends_as_a_transient_error(key):
    google = Google([google_error(429, "Quota exceeded")] * 4)
    with pytest.raises(google_sheets.SheetsError) as error:
        await google.client().batch_get(SHEET_ID, ["'Untitled'"])
    assert error.value.transient and "HTTP 429" in str(error.value)
    assert google.sleeps == [1.0, 2.0, 4.0] and len(google.requests) == 4


async def test_network_timeout_is_retried(key):
    google = Google([httpx.ConnectTimeout("timeout"), values(["ok"])])
    assert await google.client().batch_get(SHEET_ID, ["'Untitled'"]) == [[["ok"]]]
    assert google.sleeps == [1.0]


async def test_401_takes_a_fresh_token_once(key):
    google = Google([google_error(401, "Request had invalid authentication credentials."), values(["ok"])])
    assert await google.client().batch_get(SHEET_ID, ["'Untitled'"]) == [[["ok"]]]
    assert google.tokens_issued == 2 and google.sleeps == []


@pytest.mark.parametrize(
    "json_value,b64_value,expected",
    [
        ("", "", "ключ сервисного аккаунта не задан"),
        ("{не json " + "x" * 10, "", "не разбирается как JSON"),
        ("", "!!!не base64!!!", "не base64"),
        (json.dumps({"type": "authorized_user", "client_id": "1"}), "", "не JSON-ключ сервисного аккаунта"),
        (sa_json(private_key="-----BEGIN PRIVATE KEY-----\nmusor\n-----END PRIVATE KEY-----\n"), "",
         "закрытый ключ сервисного аккаунта не читается"),
    ],
)
async def test_bad_key_is_explained_without_leaking_it(monkeypatch, caplog, json_value, b64_value, expected):
    monkeypatch.setattr(settings, "google_sa_json", json_value)
    monkeypatch.setattr(settings, "google_sa_json_b64", b64_value)
    google = Google()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(google_sheets.SheetsError) as error:
            await google.client().batch_get(SHEET_ID, ["'Untitled'"])
    assert expected in str(error.value)
    # Исключение без цепочки: в `__context__` мог бы оказаться кусок ключа.
    assert error.value.__cause__ is None
    assert error.value.__context__ is None or error.value.__suppress_context__
    assert google.requests == []
    assert KEY_FRAGMENT not in str(error.value) and KEY_FRAGMENT not in caplog.text


async def test_revoked_key_is_explained(key, caplog):
    google = Google(token_status=400)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(google_sheets.SheetsError) as error:
            await google.client().batch_get(SHEET_ID, ["'Untitled'"])
    assert "не выдал токен" in str(error.value) and ACCOUNT in str(error.value)
    assert "invalid_grant" in str(error.value) and not error.value.transient
    assert KEY_FRAGMENT not in str(error.value) and KEY_FRAGMENT not in caplog.text
