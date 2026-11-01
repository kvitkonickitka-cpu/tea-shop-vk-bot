"""Тестовые артикулы и готовность к продаже (ревизия 24.10.2026).

До старта продаж покупателям не продаём ничего: тестовый артикул (`shop.catalog.is_test`)
видят и покупают только аккаунты из TEST_VK_IDS, остальным бот говорит, когда
магазин открывается. Настоящий товар форма включает в продажу только готовым:
GTIN, цена, описание, «Как заваривать» и не тестовая декларация.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.modules.catalog import service as catalog_service
from app.modules.catalog import store
from app.modules.dialog import test_mode
from tests.test_catalog_db import GTIN_A, GTIN_B, add, shop  # noqa: F401
from tests.test_fin_model import fin  # noqa: F401

TEST_PEER, CUSTOMER = 111449231, 5550001


def _items():
    rows = [{"sku_id": "P-TEST-100", "kind": "пачка", "name": "Тестовый улун", "description": "Для проверки бота",
             "net_weight_g": 100, "raw_material": "Улун", "gtin": GTIN_A, "price": 390, "on_sale": True,
             "vk_market_url": None, "recommend_with": [], "brew_guide": "85 °C", "video_url": None, "is_new": False,
             "synonyms": [], "category": None, "is_test": True},
            {"sku_id": "P-REAL-100", "kind": "пачка", "name": "Настоящий пуэр", "description": "Пуэр",
             "net_weight_g": 100, "raw_material": "Пуэр", "gtin": GTIN_B, "price": 990, "on_sale": False,
             "vk_market_url": None, "recommend_with": [], "brew_guide": "95 °C", "video_url": None, "is_new": False,
             "synonyms": [], "category": None, "is_test": False}]
    return store.build_items(rows, {}, {})


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(catalog_service, "from_db", lambda: True)
    monkeypatch.setattr(store, "current_items", lambda: [dict(i) for i in _items()])

    async def is_test(peer_id):
        return peer_id == TEST_PEER

    monkeypatch.setattr(test_mode, "is_test", is_test)


async def test_test_article_is_for_test_accounts_only(catalog):
    async with catalog_service.viewer(CUSTOMER):
        items = {i["sku_id"]: i for i in catalog_service.load_items()}
        assert not items["P-TEST-100"]["in_stock"] and items["P-TEST-100"]["hidden"]
        assert catalog_service.shop_closed()
        context = await catalog_service.build_catalog_context()
    # Обычному покупателю — что магазин открывается, без тестовых и неготовых товаров.
    assert "открывается 1 декабря" in context and "подписаться на новости" in context
    assert "Тестовый улун" not in context and "Настоящий пуэр" not in context

    async with catalog_service.viewer(TEST_PEER):
        items = {i["sku_id"]: i for i in catalog_service.load_items()}
        assert items["P-TEST-100"]["in_stock"] and not catalog_service.shop_closed()
        context = await catalog_service.build_catalog_context()
    assert "Тестовый улун (390 ₽" in context and "открывается" not in context
    # Вне хода клиента (сборка, чек) — как у покупателя: тестовый артикул не продаётся.
    assert not {i["sku_id"]: i for i in catalog_service.load_items()}["P-TEST-100"]["in_stock"]


async def test_real_article_goes_on_sale_only_when_ready(shop):
    await add(shop, on_sale=False, description="", brew_guide="")
    with pytest.raises(asyncpg.RaiseError, match="без описания"):
        await shop.execute("update shop.catalog set on_sale = true")
    await shop.execute("update shop.catalog set description = 'Улун'")
    with pytest.raises(asyncpg.RaiseError, match="Как заваривать"):
        await shop.execute("update shop.catalog set on_sale = true")
    # Тестовому — можно и без текстов: им проверяют бота.
    await add(shop, "P-TEST-35", name="Тест 35", net_weight_g=35, gtin=GTIN_B, on_sale=True, is_test=True,
              description="", brew_guide="")
    reasons = {r["sku_id"]: r["reasons"] for r in await shop.fetch("select * from shop.v_catalog_readiness")}
    assert reasons["P-TGI-ECO-100"] == ["нет «Как заваривать»"]


async def test_test_declaration_keeps_real_articles_off_sale(fin):
    sku = await fin.fetchval("select sku_id from shop.catalog where kind = 'пачка' order by sku_id limit 1")
    await fin.execute("update shop.catalog set gtin = $2, description = 'Улун', brew_guide = '85 °C' where sku_id = $1",
                      sku, GTIN_A)
    # Декларация в вводных — тестовая (миграция 015): настоящий товар не включить.
    with pytest.raises(asyncpg.RaiseError, match="декларация"):
        await fin.execute("update shop.catalog set on_sale = true where sku_id = $1", sku)
    assert "декларация тестовая" in await fin.fetchval(
        "select reasons from shop.v_catalog_readiness where sku_id = $1", sku)
    await fin.execute("update shop.catalog set is_test = true, on_sale = true where sku_id = $1", sku)
    await fin.execute("update shop.catalog set is_test = false, on_sale = false where sku_id = $1", sku)
    await fin.execute("insert into fin.inputs (key, value_text, entered_by) values "
                      "('chz_declaration_number', 'ЕАЭС N RU Д-CN.РА01.В.12345/26', 'Никита')")
    await fin.execute("update shop.catalog set on_sale = true where sku_id = $1", sku)
    assert await fin.fetchval("select reasons from shop.v_catalog_readiness where sku_id = $1", sku) == []


async def test_public_investor_demo_can_be_disabled(catalog, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "investor_demo_enabled", True)
    async with catalog_service.viewer(CUSTOMER):
        items = {i["sku_id"]: i for i in catalog_service.load_items()}
        assert items["P-TEST-100"]["in_stock"]
        assert not items["P-REAL-100"]["in_stock"]
        assert not catalog_service.shop_closed()
        context = await catalog_service.build_catalog_context()
        assert "Демонстрационный режим" in context and "Тестовый улун" in context
        assert "продажи начинаются" not in context
    monkeypatch.setattr(settings, "investor_demo_enabled", False)
    async with catalog_service.viewer(CUSTOMER):
        assert catalog_service.shop_closed()
        assert "продажи начинаются" in await catalog_service.build_catalog_context()


async def test_demo_orders_stay_test_after_switch_off(clean, monkeypatch):
    from app.core.config import settings
    from app.modules.orders import repository
    from app.modules.orders.state import OrderDraft
    monkeypatch.setattr(settings, "investor_demo_enabled", True)
    draft = OrderDraft(items=[{"name":"Тестовый улун","price":390,"quantity":1}],items_total=390)
    order = await repository.save_order(CUSTOMER, draft)
    assert order.is_test and order.details["investor_demo"] is True
    monkeypatch.setattr(settings, "investor_demo_enabled", False)
    saved = await repository.by_id(order.id)
    assert saved.is_test
    regular = await repository.save_order(CUSTOMER, draft)
    assert not regular.is_test and "investor_demo" not in regular.details


async def test_demo_stock_override_only_for_on_sale_test_sku(catalog, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "investor_demo_enabled", True)
    source = _items()
    for item in source:
        item["in_stock"] = False
        item["stock_ok"] = False
        item["on_sale"] = item["is_test"]
    monkeypatch.setattr(store, "current_items", lambda: source)
    async with catalog_service.viewer(CUSTOMER):
        items = {i["sku_id"]: i for i in catalog_service.load_items()}
        assert items["P-TEST-100"]["in_stock"]
        assert not items["P-TEST-100"]["stock_ok"]
        assert not items["P-REAL-100"]["in_stock"]
    assert all(not item["in_stock"] for item in source)
    source[0]["on_sale"] = False
    async with catalog_service.viewer(CUSTOMER):
        assert not catalog_service.load_items()[0]["in_stock"]
