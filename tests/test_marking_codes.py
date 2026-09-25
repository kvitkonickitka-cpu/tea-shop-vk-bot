"""Коды маркировки: разбор и пул из выгрузки СУЗ."""

from __future__ import annotations

import pytest

from app.modules.catalog import service as catalog_service
from app.modules.marking import codes, pool
from app.modules.marking.codes import GS

GTIN = "04606203099221"
SIG44 = "W+fZ3/nN3/qWGc0cITw4SN1h5SfKWGQ1hG/E+f/2vBA="
SHORT = f"01{GTIN}21ABC123def456g{GS}93dGVz"
LONG = f"01{GTIN}21XYZ{GS}91EE06{GS}92{SIG44}"


def test_short_code_with_gs():
    parsed = codes.parse(SHORT)
    assert (parsed.gtin, parsed.serial, parsed.crypto) == (GTIN, "ABC123def456g", "93")
    assert parsed.code == SHORT and not parsed.restored


def test_long_code_with_gs():
    parsed = codes.parse(LONG)
    assert (parsed.serial, parsed.crypto) == ("XYZ", "91+92")
    assert parsed.code == LONG


@pytest.mark.parametrize(
    "raw",
    [
        "]d2" + SHORT,                 # идентификатор символики от сканера
        "\xe8" + SHORT,                # FNC1 как символ 232
        GS + SHORT,                    # ведущий FNC1 как GS
        SHORT.replace(GS, "<GS>"),     # разделитель, вписанный руками
        f"(01){GTIN}(21)ABC123def456g(93)dGVz",  # человекочитаемый вид
        "  " + SHORT + "\r\n",
    ],
)
def test_scanner_and_manual_spellings(raw):
    parsed = codes.parse(raw)
    assert parsed.code == SHORT
    assert parsed.serial == "ABC123def456g"


def test_code_without_gs_is_restored():
    parsed = codes.parse(SHORT.replace(GS, ""))
    assert parsed.restored
    assert parsed.code == SHORT
    # «93» внутри серийника не сбивает: хвост фиксированной длины стоит последним.
    tricky = codes.parse(f"01{GTIN}21AB93CDEFGHIJK93dGVz")
    assert tricky.serial == "AB93CDEFGHIJK"
    long = codes.parse(LONG.replace(GS, ""))
    assert long.code == LONG and long.crypto == "91+92"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "Пустой"),
        ("4606203099221", "не код маркировки"),
        (f"01{GTIN}21ABC123def456g", "криптохвоста"),
        (f"01{GTIN}21ABC123def456g{GS}10LOT1", "криптохвоста"),
        (f"01{GTIN[:-1]}0" + f"21ABC{GS}93dGVz", "Контрольная цифра"),
        (f"01{GTIN}21ABC123def456gXYZWVUTSRQ", "разделителей"),
    ],
)
def test_broken_codes(raw, expected):
    with pytest.raises(codes.CodeError) as error:
        codes.parse(raw)
    assert expected in str(error.value)


def test_gtin_helpers():
    assert codes.gtin_is_valid(GTIN)
    assert not codes.gtin_is_valid("04606203099222")
    assert codes.normalize_gtin("4606203099221") == GTIN
    assert codes.normalize_gtin(" 0460 6203 0992 21 ") == GTIN


def test_catalog_gtin_lookup():
    items = [
        {"name": "Те Гуань Инь (тест)", "gtin": "4606203099221"},
        {"name": "Да Хун Пао", "gtin": "123"},
        {"name": "Шу Пуэр"},
    ]
    assert catalog_service.gtin_for("Те Гуань Инь (тест)", items) == GTIN
    assert catalog_service.gtin_for("Те Гуань Инь", items) == GTIN
    assert catalog_service.gtin_for("Да Хун Пао", items) == ""   # записан с ошибкой
    assert catalog_service.gtin_for("Шу Пуэр", items) == ""
    assert catalog_service.marking_configured(items)
    assert not catalog_service.marking_configured(items[1:])


def test_export_parsing():
    serial_with_comma = "Ab,c;d"
    export = "\n".join([
        "TOKEN-LINE",
        SHORT,
        f'"01{GTIN}21{serial_with_comma}{GS}93AAAA",2026-09-01,выпущен',
        "мусор",
        "",
        LONG,
    ])
    parsed, errors = pool.parse_export(export, skip="TOKEN-LINE")
    assert [c.serial for c in parsed] == ["ABC123def456g", serial_with_comma, "XYZ"]
    assert len(errors) == 1 and "строка 4" in errors[0]
    assert all("TOKEN" not in e for e in errors)


async def test_import_is_idempotent(clean):
    export = f"{SHORT}\n{LONG}\n"
    first = await pool.import_codes(export)
    again = await pool.import_codes(export + SHORT.replace(GS, "") + "\n")
    assert first["добавлено"] == 2
    # Тот же код без разделителей — та же пара GTIN + серийный номер.
    assert again["добавлено"] == 0 and again["уже были"] == 3
    assert await pool.pool_imported()


async def test_no_pool_before_import(clean):
    assert not await pool.pool_imported()


async def test_import_endpoint_takes_token_and_file(clean, monkeypatch):
    """Как шлёт scripts/api.sh: токен первой строкой, файл следом."""
    import httpx

    from app.core.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "internal_api_token", "secret-token-123")
    body = ("secret-token-123\n" + SHORT + "\n" + LONG + "\n").encode("utf-8")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        denied = await http.post("/internal/codes/import", content=SHORT.encode())
        answer = await http.post("/internal/codes/import", content=body)
    assert denied.status_code == 403
    assert answer.json()["добавлено"] == 2
    assert answer.json()["ошибок"] == 0
