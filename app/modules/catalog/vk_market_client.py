from __future__ import annotations

import httpx

from app.core.config import settings

VK_API_URL = "https://api.vk.com/method"

_group_numeric_id: int | None = None


async def _get_group_numeric_id(client: httpx.AsyncClient) -> int:
    global _group_numeric_id
    if _group_numeric_id is not None:
        return _group_numeric_id

    response = await client.get(
        f"{VK_API_URL}/groups.getById",
        params={
            "group_id": settings.vk_group_id,
            "access_token": settings.vk_market_user_token,
            "v": settings.vk_api_version,
        },
    )
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"VK API error (groups.getById): {data['error']}")

    groups = data["response"]
    if isinstance(groups, dict):
        groups = groups["groups"]
    _group_numeric_id = groups[0]["id"]
    return _group_numeric_id


async def fetch_market_items() -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        group_id = await _get_group_numeric_id(client)
        response = await client.get(
            f"{VK_API_URL}/market.get",
            params={
                "owner_id": -group_id,
                "count": 200,
                "access_token": settings.vk_market_user_token,
                "v": settings.vk_api_version,
            },
        )
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"VK API error (market.get): {data['error']}")
        return data["response"]["items"]
