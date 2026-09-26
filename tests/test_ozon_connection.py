"""Одно соединение с Ozon на все вызовы: иначе каждый запрос — через редирект."""

from __future__ import annotations

from app.modules.delivery import ozon_client


async def test_calls_share_one_client():
    first = ozon_client._shared_client()
    assert ozon_client._shared_client() is first

    await first.aclose()
    # Закрытый клиент заменяется, а не роняет следующий вызов.
    second = ozon_client._shared_client()
    assert second is not first and not second.is_closed
    await second.aclose()
