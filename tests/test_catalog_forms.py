"""Формы вкладки «Каталог» в Metabase: их SQL выполняется под ролью форм.

Metabase в тестах нет. Поэтому сборка форм (`dash_build.catalog_forms`)
запускается с подменённым API, а SQL каждой формы подставляется так, как это
делает Metabase: заполненное поле — строкой или числом, пустое необязательное —
вырезается вместе со своими [[…]]. Потом запрос выполняется в тестовой базе
под ролью metabase_fin — с её правами и триггерами каталога.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import asyncpg
import pytest

from tests.test_catalog_db import GTIN_A, GTIN_B, add, shop  # noqa: F401

METABASE = Path(__file__).resolve().parents[1] / "analytics" / "metabase"


@pytest.fixture
def forms(monkeypatch):
    """Собрать формы каталога, не ходя в Metabase: id действия → тело запроса."""
    created = {"actions": [], "cards": [], "fields": []}
    lists = {"rows": []}

    def call(method, path, body=None):
        if path == "/dataset":
            return {"data": {"rows": lists["rows"]}}
        if path.endswith("/metadata"):
            return {"tables": [{"schema": "shop", "name": "catalog",
                                "fields": [{"id": 7, "name": "sku_id", "semantic_type": "type/PK"}]}]}
        if path == "/field/7":
            created["fields"].append(body)
            return {}
        if path == "/action":
            created["actions"].append(body)
            return {"id": len(created["actions"])}
        if path == "/card":
            created["cards"].append(body)
            return {"id": 500 + len(created["cards"])}
        raise AssertionError(f"неожиданный вызов {method} {path}")

    fake = types.ModuleType("mbapi")
    fake.BASE, fake.call = "https://stats.example/api", call
    monkeypatch.setitem(sys.modules, "mbapi", fake)
    monkeypatch.syspath_prepend(str(METABASE))
    for name in ("fin_build", "dash_build"):
        sys.modules.pop(name, None)
    import dash_build  # noqa: PLC0415
    import fin_build  # noqa: PLC0415

    fin_build.SKUS = ["Те Гуань Инь ЭКО (XFI0044)"]
    lists["rows"] = [["P-TGI-ECO-100 · Те Гуань Инь", "пачка"], ["P-TGI-ECO-35 · ТГИ 35", "пачка"],
                     ["S-ENTRY · Набор «Точка входа»", "набор"]]
    models = dash_build.catalog_forms(1, 9, {"shop.catalog": 11, "shop.bundle_items": 12}, {})
    yield created, models
    for name in ("fin_build", "dash_build"):
        sys.modules.pop(name, None)


def _literal(value) -> str:
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def render(sql: str, values: dict) -> str:
    """Подстановка параметров как у Metabase: [[…]] без значения исчезает."""
    def optional(match):
        names = re.findall(r"\{\{(\w+)\}\}", match.group(1))
        return match.group(1) if all(values.get(n) not in (None, "") for n in names) else ""

    sql = re.sub(r"\[\[(.*?)\]\]", optional, sql, flags=re.S)
    return re.sub(r"\{\{(\w+)\}\}", lambda m: _literal(values[m.group(1)]), sql)


def by_name(created, name):
    return next(a for a in created["actions"] if a.get("name") == name)


async def run_as_form(conn, sql):
    async with conn.transaction():
        await conn.execute("set local role metabase_fin")
        await conn.execute(sql)


def test_catalog_models_can_be_edited_but_not_deleted(forms):
    created, models = forms
    # sku_id больше не ключ для Metabase: с двумя PK карточка строки ломается.
    assert created["fields"] == [{"semantic_type": None}]
    implicit = [a for a in created["actions"] if a.get("type") == "implicit"]
    assert {a["kind"] for a in implicit} == {"row/update"}
    assert {a["model_id"] for a in implicit} == {models["shop.catalog"], models["shop.bundle_items"]}
    queries = [a for a in created["actions"] if a.get("type") == "query"]
    assert {a["name"] for a in queries} == {"Изменить цену", "Добавить артикул", "Снять с продажи / вернуть",
                                            "Состав сета/набора"}
    for action in queries:
        assert "delete" not in action["dataset_query"]["native"]["query"].lower()
    # Длинные поля — многострочные.
    add_form = by_name(created, "Добавить артикул")["visualization_settings"]["fields"]
    assert add_form["description"]["inputType"] == "text" and add_form["brew"]["inputType"] == "text"


async def test_price_form(shop, forms):
    created, _ = forms
    await add(shop)
    sql = by_name(created, "Изменить цену")["dataset_query"]["native"]["query"]
    await run_as_form(shop, render(sql, {"sku": "P-TGI-ECO-100 · Те Гуань Инь", "price": 420,
                                         "reason": "новый прайс", "entered_by": "Илья"}))
    row = await shop.fetchrow("select changed_by, note, old_value, new_value from shop.catalog_changes where field = 'price'")
    assert tuple(row) == ("Илья", "новый прайс", "390", "420")
    with pytest.raises(asyncpg.RaiseError, match="30 %"):
        await run_as_form(shop, render(sql, {"sku": "P-TGI-ECO-100 · Те Гуань Инь", "price": 900,
                                             "reason": "опечатка?", "entered_by": "Илья"}))
    await run_as_form(shop, render(sql, {"sku": "P-TGI-ECO-100 · Те Гуань Инь", "price": 900, "confirm": "да",
                                         "reason": "премиум-партия", "entered_by": "Никита"}))
    assert await shop.fetchval("select price from shop.catalog") == 900


async def test_add_sale_and_bundle_forms(shop, forms):
    created, _ = forms
    add_sql = by_name(created, "Добавить артикул")["dataset_query"]["native"]["query"]
    await run_as_form(shop, render(add_sql, {
        "sku_id": "p-tgi-eco-100", "kind": "пачка", "name": "Те Гуань Инь", "weight": "100",
        "raw": "Те Гуань Инь ЭКО (XFI0044)", "price": "390", "description": "Улун\nс сиренью",
        "gtin": GTIN_A, "entered_by": "Никита"}))
    await run_as_form(shop, render(add_sql, {
        "sku_id": "P-TGI-ECO-35", "kind": "пачка", "name": "ТГИ 35", "weight": "35",
        "raw": "Те Гуань Инь ЭКО (XFI0044)", "entered_by": "Никита"}))
    await run_as_form(shop, render(add_sql, {
        "sku_id": "S-ENTRY", "kind": "набор", "name": "Набор «Точка входа»", "price": "1490", "gtin": GTIN_B,
        "entered_by": "Никита"}))
    row = await shop.fetchrow("select * from shop.catalog where sku_id = 'P-TGI-ECO-100'")
    assert (row["price"], row["on_sale"], row["description"], row["net_weight_g"]) == (390, False, "Улун\nс сиренью", 100)
    assert await shop.fetchval("select price from shop.catalog where sku_id = 'P-TGI-ECO-35'") is None

    sale_sql = by_name(created, "Снять с продажи / вернуть")["dataset_query"]["native"]["query"]
    await run_as_form(shop, render(sale_sql, {"sku": "P-TGI-ECO-100 · Те Гуань Инь", "state": "в продаже",
                                              "entered_by": "Илья"}))
    assert await shop.fetchval("select on_sale from shop.catalog where sku_id = 'P-TGI-ECO-100'") is True
    with pytest.raises(asyncpg.RaiseError, match="без"):
        await run_as_form(shop, render(sale_sql, {"sku": "P-TGI-ECO-35 · ТГИ 35", "state": "в продаже",
                                                  "entered_by": "Илья"}))

    bundle_sql = by_name(created, "Состав сета/набора")["dataset_query"]["native"]["query"]
    values = {"bundle": "S-ENTRY · Набор «Точка входа»", "item": "P-TGI-ECO-35 · ТГИ 35", "entered_by": "Илья"}
    await run_as_form(shop, render(bundle_sql, {**values, "quantity": 2, "op": "добавить"}))
    await run_as_form(shop, render(bundle_sql, {**values, "op": "убрать"}))
    part = await shop.fetchrow("select quantity, active from shop.bundle_items")
    assert (part["quantity"], part["active"]) == (1, False)
    assert await shop.fetchval("select count(*) from shop.catalog_changes where entity = 'состав'") == 2
