import random

import httpx

from app.core.config import settings

VK_API_URL = "https://api.vk.com/method"


async def send_message(peer_id: int, text: str) -> None:
    params = {
        "access_token": settings.vk_access_token,
        "v": settings.vk_api_version,
        "peer_id": peer_id,
        "message": text,
        "random_id": random.getrandbits(31),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{VK_API_URL}/messages.send", data=params)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"VK API error: {data['error']}")
