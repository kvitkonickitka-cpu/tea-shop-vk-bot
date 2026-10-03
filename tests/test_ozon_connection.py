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
