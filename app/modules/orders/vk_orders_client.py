import httpx

from app.core.config import settings

VK_API_URL = "https://api.vk.com/method"


def _params(order_id: int, user_id: int | None, **extra) -> dict:
    # Заказ в ВК принадлежит покупателю: без его user_id методы заказа
    # ищут заказ у владельца токена и не находят.
    params = {"order_id": order_id, "access_token": settings.vk_access_token,
              "v": settings.vk_api_version, **extra}
    if user_id:
        params["user_id"] = user_id
    return params


async def get_order(order_id: int, user_id: int | None = None) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{VK_API_URL}/market.getOrderById", params=_params(order_id, user_id, extended=1),
        )
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"VK API error (market.getOrderById): {data['error']}")
        # Заказ приходит обёрнутым: {"order": {...}} (живой ответ 04.10.2026 —
        # в логе «поля ['order']»). Без разворота не находился user_id, и заказ
        # терялся с «Order … has no user_id».
        response = data["response"]
        return response.get("order", response) if isinstance(response, dict) else response


async def get_order_items(order_id: int, user_id: int | None = None) -> list[dict]:
    """Состав заказа из витрины.

    Отдельным запросом: `market.getOrderById` возвращает сам заказ, но не
    товары в нём — за ними ВК посылает в `market.getOrderItems`.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{VK_API_URL}/market.getOrderItems", params=_params(order_id, user_id),
        )
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"VK API error (market.getOrderItems): {data['error']}")
        return (data.get("response") or {}).get("items") or []
