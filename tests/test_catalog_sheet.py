"""Каталог из Google Таблицы: хорошая таблица применяется, плохая — нет."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.modules.catalog import service as catalog_service, sheet
from app.modules.catalog.models import CatalogSnapshot

GOOD = (
    "Название,Цена,Фасовки,В наличии,GTIN,Ссылка,Описание\n"
    "Те Гуань Инь 100 г,\"1 200,00 ₽\",100 г,да,4600000000008,https://vk.ru/market/x,Улун\n"
    ",,,,,,\n"
    "Да Хун Пао 50 г,650,50 г,нет,,,\n"
)


@pytest.fixture
def sheet_url(monkeypatch):
    monkeypatch.setattr(settings, "catalog_sheet_csv_url", "https://docs.google.com/pub?output=csv")
    sheet._remember(None)
    sheet._memory_loaded_at = 0.0
    yield
    sheet._remember(None)
    sheet._memory_loaded_at = 0.0


_REAL_CLIENT = httpx.AsyncClient


def serve(monkeypatch, text: str, status: int = 200):
    real = _REAL_CLIENT

    def client(**kwargs):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(status, content=text.encode("utf-8"))
        )
        return real(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(sheet.httpx, "AsyncClient", client)


@pytest.fixture
def manager(monkeypatch):
    sent = []

    async def notify(kind, text, **kwargs):
        sent.append(text)
        return True

    monkeypatch.setattr(sheet.manager_messages, "notify", notify)
    return sent


def test_parse_good_sheet():
    parsed = sheet.parse_csv(GOOD)
    assert parsed.errors == []
    first, second = parsed.items
    assert first["name"] == "Те Гуань Инь 100 г" and first["price"] == 1200
    assert first["gtin"] == "04600000000008" and first["package_sizes"] == ["100 г"]
    assert first["in_stock"] is True and second["in_stock"] is False
    assert second["gtin"] == ""


@pytest.mark.parametrize(
    "csv_text,error",
    [
        ("Товар,Фасовки\nЧай,50 г\n", "нет столбцов: Цена"),
        ("Название,Цена\nЧай,\n", "не число больше нуля"),
        ("Название,Цена,GTIN\nЧай,500,4600000000009\n", "контрольная цифра"),
        ("Название,Цена,В наличии\nЧай,500,может быть\n", "нужно «да» или «нет»"),
        ("Название,Цена\nЧай,500\nчай,600\n", "уже есть выше"),
        ("Название,Цена\n", "нет ни одного товара"),
    ],
)
def test_parse_reports_errors(csv_text, error):
    parsed = sheet.parse_csv(csv_text)
    assert any(error in e for e in parsed.errors), parsed.errors


async def test_good_sheet_becomes_the_catalog(clean, sheet_url, monkeypatch, manager):
    serve(monkeypatch, GOOD)

    result = await sheet.refresh()

    assert result["applied"] and result["товаров"] == 2
    names = [item["name"] for item in catalog_service.load_items()]
    assert names == ["Те Гуань Инь 100 г", "Да Хун Пао 50 г"]
    assert catalog_service.gtin_for("Те Гуань Инь 100 г") == "04600000000008"
    assert manager == []

    # Другой контейнер (пустая память) подтягивает ту же версию из базы.
    sheet._remember(None)
    sheet._memory_loaded_at = 0.0
    await sheet.ensure_fresh()
    assert [i["name"] for i in catalog_service.load_items()] == names


async def test_broken_sheet_keeps_the_last_good_version(clean, sheet_url, monkeypatch, manager):
    serve(monkeypatch, GOOD)
    await sheet.refresh()

    broken = "Название,Цена\nТе Гуань Инь 100 г,\n"
    serve(monkeypatch, broken)
    result = await sheet.refresh()
    await sheet.refresh()  # та же ошибка на следующем тике

    assert result["applied"] is False
    assert [i["name"] for i in catalog_service.load_items()][0] == "Те Гуань Инь 100 г"
    assert len(manager) == 1 and "не число больше нуля" in manager[0]

    serve(monkeypatch, GOOD)
    assert (await sheet.refresh())["applied"]
    async with clean() as session:
        row = await session.get(CatalogSnapshot, 1)
    assert row.last_error is None


async def test_page_instead_of_csv_is_explained(clean, sheet_url, monkeypatch, manager):
    serve(monkeypatch, "<!DOCTYPE html><html><body>Google Sheets</body></html>")
    result = await sheet.refresh()
    assert result["applied"] is False
    assert "Опубликовать в интернете" in manager[0]


async def test_google_down_changes_nothing(clean, sheet_url, monkeypatch, manager):
    serve(monkeypatch, "oops", status=500)
    result = await sheet.refresh()
    assert "failed" in result and manager == []
    # Таблица ни разу не читалась — бот продаёт по catalog.json.
    assert catalog_service.load_items()[0]["name"] == "Те Гуань Инь (тест)"


async def test_without_url_the_image_catalog_is_used(monkeypatch):
    monkeypatch.setattr(settings, "catalog_sheet_csv_url", "")
    assert "skipped" in await sheet.refresh()
    assert catalog_service.load_items()[0]["name"] == "Те Гуань Инь (тест)"


async def test_other_container_sees_the_new_sheet_within_seconds(clean, sheet_url, monkeypatch, manager):
    """27.09.2026: таблицу дополнили, а бот ещё минуту отвечал по старой."""
    serve(monkeypatch, GOOD)
    await sheet.refresh()
    assert len(catalog_service.load_items()) == 2

    # Другой контейнер (или команда catalog/sheet) положил в базу новую версию.
    newer = GOOD + "Шу Пуэр 100 г,900,100 г,да,,,\n"
    async with clean() as session:
        row = await session.get(CatalogSnapshot, 1)
        row.items = sheet.parse_csv(newer).items
        row.source_hash = "другой"
        await session.commit()

    # В пределах пяти секунд — без похода в базу.
    await sheet.ensure_fresh()
    assert len(catalog_service.load_items()) == 2

    sheet._memory_loaded_at -= 10
    await sheet.ensure_fresh()
    assert [i["name"] for i in catalog_service.load_items()][-1] == "Шу Пуэр 100 г"
