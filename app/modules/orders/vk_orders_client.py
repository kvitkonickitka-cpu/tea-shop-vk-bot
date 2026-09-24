import httpx

from app.core.config import settings

VK_API_URL = "https://api.vk.com/method"


async def get_order(order_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{VK_API_URL}/market.getOrderById",
            params={
                "order_id": order_id,
                "extended": 1,
                "access_token": settings.vk_access_token,
                "v": settings.vk_api_version,
            },
        )
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"VK API error (market.getOrderById): {data['error']}")
        return data["response"]


async def get_order_items(order_id: int) -> list[dict]:
    """Состав заказа из витрины.

    Отдельным запросом: `market.getOrderById` возвращает сам заказ, но не
    товары в нём — за ними ВК посылает в `market.getOrderItems`.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{VK_API_URL}/market.getOrderItems",
            params={
                "order_id": order_id,
                "access_token": settings.vk_access_token,
                "v": settings.vk_api_version,
            },
        )
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"VK API error (market.getOrderItems): {data['error']}")
        return (data.get("response") or {}).get("items") or []
