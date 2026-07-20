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
