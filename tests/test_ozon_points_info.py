"""Пачка пунктов, где часть уже закрыта, не роняет выгрузку каталога."""

from __future__ import annotations

import logging

from app.modules.delivery import ozon_client


async def test_missing_points_are_skipped(monkeypatch):
    asked = []

    async def fake_call(path, payload):
        ids = payload["delivery_point_ids"]
        asked.append(list(ids))
        if 2 in ids:
            raise ozon_client.OzonError(
                "Ozon отказал на /v1/delivery-point/info — HTTP 404; ?: "
                "Не найдены пункты выдачи: 2,5."
            )
        return {"delivery_points": [{"delivery_point_id": i, "full_address": f"адрес {i}"} for i in ids]}

    monkeypatch.setattr(ozon_client, "call", fake_call)
    points = await ozon_client.delivery_points_info([1, 2, 3, 5])
    assert [p.id for p in points] == [1, 3]
    assert asked == [[1, 2, 3, 5], [1, 3]]


async def test_other_errors_still_raise(monkeypatch):
    async def fake_call(path, payload):
        raise ozon_client.OzonError("HTTP 500")

    monkeypatch.setattr(ozon_client, "call", fake_call)
    try:
        await ozon_client.delivery_points_info([1])
    except ozon_client.OzonError:
        pass
    else:
        raise AssertionError("ошибку проглотили")


def test_request_urls_are_not_logged():
    import app.main  # noqa: F401 — настраивает логи

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
