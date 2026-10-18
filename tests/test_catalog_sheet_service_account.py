"""Каталог через сервисный аккаунт: тот же разбор, громкие ошибки, без отката.

Sheets API подменён: настоящих запросов к таблице нет.
"""

from __future__ import annotations

import csv
import io

import httpx
import pytest

from app.core import google_sheets
from app.core.config import settings
from app.modules.catalog import service as catalog_service, sheet

NBSP = " "

# Как строки отдаёт Sheets API с FORMATTED_VALUE: числа в русском формате,
# пустые ячейки в конце строки обрезаны, многострочное описание.
API_ROWS = [
    ["Название", "Цена", "Фасовки", "В наличии", "GTIN", "Ссылка", "Описание", "С чем советуем", "Новинка"],
    ["Те Гуань Инь", f"1{NBSP}990", "100 г", "да", "", "https://vk.ru/market/x", "Улун.\nВкус сирени.",
     "Да Хун Пао", "да"],
    ["Да Хун Пао", "1 200,00 ₽", "100 г; 50 г", "нет"],
    ["Дянь Хун", "1,5"],
]


def published_csv(rows: list[list[str]]) -> str:
    """Тот же лист так, как его публикует «Опубликовать в интернете → CSV».

    Каждая строка на всю ширину таблицы, плюс пустая строка в конце листа.
    """
    width = max(len(row) for row in rows)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for row in rows + [[]]:
        writer.writerow(row + [""] * (width - len(row)))
    return out.getvalue()


class FakeSheets:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def batch_get(self, spreadsheet_id, ranges, **kwargs):
        self.calls.append((spreadsheet_id, ranges))
        if isinstance(self.answer, Exception):
            raise self.answer
        return [self.answer]

    account = "tea-bot-sheets-reader@test-project.iam.gserviceaccount.com"


@pytest.fixture
def service_account(monkeypatch):
    monkeypatch.setattr(settings, "sheets_auth_mode", "service_account")
    monkeypatch.setattr(settings, "catalog_sheet_id", "sheet-id-for-tests")
    monkeypatch.setattr(settings, "catalog_sheet_tab", "Untitled")
    # Публичная ссылка задана — и не должна использоваться ни при успехе, ни при сбое.
    monkeypatch.setattr(settings, "catalog_sheet_csv_url", "https://docs.google.com/pub?output=csv")

    def no_public_download(**kwargs):
        raise AssertionError("в режиме service_account бот не должен ходить по публичной ссылке")

    monkeypatch.setattr(sheet.httpx, "AsyncClient", no_public_download)
    sheet._remember(None)
    sheet._memory_loaded_at = 0.0
    yield
    sheet._remember(None)
    sheet._memory_loaded_at = 0.0


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(answer):
        holder["client"] = FakeSheets(answer)
        monkeypatch.setattr(google_sheets, "_client", holder["client"])
        return holder["client"]

    return install


@pytest.fixture
def manager(monkeypatch):
    sent = []

    async def notify(kind, text, **kwargs):
        sent.append(text)
        return True

    monkeypatch.setattr(sheet.manager_messages, "notify", notify)
    return sent


def test_parity_csv_and_api_give_the_same_catalog():
    """Один и тот же лист обоими способами — тот же каталог и тот же отпечаток."""
    from_csv = sheet._rows_from_csv(published_csv(API_ROWS))
    from_api = sheet._canonical(API_ROWS)
    assert from_csv == from_api
    assert sheet._hash_rows(from_csv) == sheet._hash_rows(from_api)

    by_csv = sheet.parse_csv(published_csv(API_ROWS))
    by_api = sheet.parse_rows(from_api)
    assert by_csv.items == by_api.items and by_csv.errors == by_api.errors == []
    tgi, dhp, dh = by_api.items
    # Русский формат чисел: пробел-разделитель тысяч, запятая — десятичная.
    assert (tgi["price"], dhp["price"], dh["price"]) == (1990, 1200, 1.5)
    assert dhp["package_sizes"] == ["100 г", "50 г"] and dhp["in_stock"] is False
    assert tgi["description"] == "Улун.\nВкус сирени." and tgi["recommended"] == ["Да Хун Пао"]
    # Обрезанные хвосты строки — это пустые ячейки, а не сдвиг столбцов.
    assert dh["in_stock"] is True and dh["gtin"] == "" and dh["link"] == ""


async def test_service_account_sheet_becomes_the_catalog(clean, service_account, fake, manager):
    client = fake(API_ROWS)
    result = await sheet.refresh()
    assert result["applied"] and result["товаров"] == 3 and manager == []
    assert client.calls == [("sheet-id-for-tests", ["'Untitled'"])]
    assert [i["name"] for i in catalog_service.load_items()] == ["Те Гуань Инь", "Да Хун Пао", "Дянь Хун"]


async def test_switching_modes_reads_the_same_version(clean, service_account, fake, manager, monkeypatch):
    """После переключения public → service_account видно, что таблица прочиталась так же."""
    monkeypatch.setattr(settings, "sheets_auth_mode", "public")
    text = published_csv(API_ROWS)

    def public_download(**kwargs):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=text.encode()))
        return _REAL_ASYNC_CLIENT(transport=transport)

    monkeypatch.setattr(sheet.httpx, "AsyncClient", public_download)
    first = await sheet.refresh()
    assert first["applied"]

    monkeypatch.setattr(settings, "sheets_auth_mode", "service_account")
    fake(API_ROWS)
    second = await sheet.refresh(force=True)
    assert second["applied"] and second["та же версия, что прочитана прошлый раз"] is True


@pytest.mark.parametrize(
    "error,expected",
    [
        (google_sheets.SheetsError("нет доступа к таблице: она не расшарена на tea-bot@x", status=403),
         "не расшарена"),
        (google_sheets.SheetsError("таблица не найдена: неверный id", status=404), "не найдена"),
        (google_sheets.SheetsError("ключ сервисного аккаунта не задан: нужна переменная GOOGLE_SA_JSON"),
         "GOOGLE_SA_JSON"),
    ],
)
async def test_broken_access_is_loud_and_keeps_the_last_version(
    clean, service_account, fake, manager, error, expected
):
    fake(API_ROWS)
    assert (await sheet.refresh())["applied"]

    fake(error)
    result = await sheet.refresh()
    await sheet.refresh()  # следующий тик с той же ошибкой — без второго 🚨
    assert result["applied"] is False and expected in result["errors"][0]
    assert len(manager) == 1 and "🚨" in manager[0] and expected in manager[0]
    # Бот на прошлой удачной версии, на публичную ссылку не откатился.
    assert len(catalog_service.load_items()) == 3


async def test_temporary_google_trouble_changes_nothing(clean, service_account, fake, manager):
    fake(google_sheets.SheetsError("Sheets API недоступен (HTTP 503) после 4 попыток", transient=True))
    result = await sheet.refresh()
    assert "failed" in result and manager == []
    # Таблица ни разу не читалась — бот продаёт по catalog.json.
    assert catalog_service.load_items()[0]["name"] == "Те Гуань Инь"


async def test_service_account_without_sheet_id_is_an_error(clean, service_account, fake, manager, monkeypatch):
    monkeypatch.setattr(settings, "catalog_sheet_id", "")
    client = fake(API_ROWS)
    result = await sheet.refresh()
    assert result["applied"] is False and "CATALOG_SHEET_ID" in result["errors"][0]
    assert client.calls == [] and len(manager) == 1


async def test_unknown_mode_is_an_error(clean, service_account, manager, monkeypatch):
    monkeypatch.setattr(settings, "sheets_auth_mode", "servise_acount")
    result = await sheet.refresh()
    assert result["applied"] is False and "нужно public или service_account" in result["errors"][0]


def test_public_mode_without_url_is_not_connected(monkeypatch):
    monkeypatch.setattr(settings, "sheets_auth_mode", "public")
    monkeypatch.setattr(settings, "catalog_sheet_csv_url", "")
    assert not sheet.connected()
    monkeypatch.setattr(settings, "sheets_auth_mode", "service_account")
    # Режим сервисного аккаунта держится за базу, даже если что-то не задано.
    assert sheet.connected()


_REAL_ASYNC_CLIENT = httpx.AsyncClient
