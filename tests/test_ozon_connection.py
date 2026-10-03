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


def _ozon(monkeypatch, answers):
    """Ozon на заглушке: токены по порядку, ответы метода — по списку."""
    import httpx

    from app.core.config import settings

    monkeypatch.setattr(settings, "ozon_client_id", "id")
    monkeypatch.setattr(settings, "ozon_client_secret", "secret")
    seen = {"tokens": 0, "auth": []}

    def handler(request):
        if str(request.url).startswith(settings.ozon_auth_url):
            seen["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"t{seen['tokens']}", "expires_in": 3600})
        seen["auth"].append(request.headers["Authorization"])
        status = answers.pop(0)
        if status == "timeout":
            raise httpx.ReadTimeout("Ozon молчит", request=request)
        if status == "disconnect":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return httpx.Response(status, json={"delivery_points": [1]} if status == 200 else {"message": "Unauthorized"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ozon_client, "_shared_client", lambda: client)
    monkeypatch.setattr(ozon_client, "_token", "revoked")
    monkeypatch.setattr(ozon_client, "_token_expires_at", 10 ** 12)
    return seen


async def test_revoked_token_is_replaced_once(monkeypatch):
    from app.modules.ops import journal

    journal._BUFFER.clear()
    seen = _ozon(monkeypatch, [401, 200])
    data = await ozon_client.call("/v1/delivery-point/list", {})
    assert data == {"delivery_points": [1]}
    assert seen["auth"] == ["Bearer revoked", "Bearer t1"]
    assert not journal._BUFFER  # прошедший повтор — не сбой


async def test_second_401_is_a_real_error(monkeypatch):
    import pytest

    from app.modules.ops import journal

    journal._BUFFER.clear()
    seen = _ozon(monkeypatch, [401, 401])
    with pytest.raises(ozon_client.OzonError, match="HTTP 401"):
        await ozon_client.call("/v1/delivery-point/list", {})
    assert seen["tokens"] == 1  # один перезапрос, без цикла
    assert journal._BUFFER[-1]["error_kind"] == "auth"
    journal._BUFFER.clear()


async def test_catalog_read_survives_one_timeout(monkeypatch):
    from app.modules.ops import journal

    journal._BUFFER.clear()
    _ozon(monkeypatch, ["timeout", 200])
    assert await ozon_client.call("/v1/delivery-point/list", {}) == {"delivery_points": [1]}
    assert not journal._BUFFER  # прошедший повтор — не сбой


async def test_two_timeouts_are_a_real_error(monkeypatch):
    import httpx
    import pytest

    from app.modules.ops import journal

    journal._BUFFER.clear()
    _ozon(monkeypatch, ["timeout", "timeout"])
    with pytest.raises(httpx.ReadTimeout):
        await ozon_client.call("/v1/delivery-point/info", {})
    assert journal._BUFFER[-1]["error_kind"] == "timeout"
    journal._BUFFER.clear()


async def test_creating_a_posting_is_never_repeated(monkeypatch):
    import httpx
    import pytest

    seen = _ozon(monkeypatch, ["timeout", 200])
    with pytest.raises(httpx.ReadTimeout):
        await ozon_client.call("/v2/order/create", {})
    assert len(seen["auth"]) == 1  # после таймаута не знаем, создал ли Ozon заказ


def test_vanished_points_are_not_a_failure():
    from app.modules.delivery.cdek_client import CdekError
    from app.modules.ops import journal

    error = ozon_client.OzonError(
        "Ozon отказал на /v1/delivery-point/info — HTTP 404: Не найдены пункты выдачи: 101, 202"
    )
    assert journal.classify(error) == ("validation", 404)
    assert journal.classify(CdekError("Не нашли город «Мсква» — HTTP 400")) == ("validation", 400)


async def test_catalog_read_survives_a_dropped_connection(monkeypatch):
    from app.modules.ops import journal

    journal._BUFFER.clear()
    _ozon(monkeypatch, ["disconnect", 200])
    assert await ozon_client.call("/v1/delivery-point/info", {}) == {"delivery_points": [1]}
    assert not journal._BUFFER
