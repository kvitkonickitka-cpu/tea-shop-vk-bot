import random

import httpx

from app.core.config import settings

VK_API_URL = "https://api.vk.com/method"


def dialog_link(peer_id: int) -> str:
    """Ссылка на переписку с клиентом в интерфейсе сообщества."""
    group_id = settings.vk_group_id
    for prefix in ("club", "public"):
        if group_id.startswith(prefix):
            group_id = group_id[len(prefix) :]
            break
    return f"https://vk.com/gim{group_id}?sel={peer_id}"


async def set_typing(peer_id: int) -> None:
    params = {
        "access_token": settings.vk_access_token,
        "v": settings.vk_api_version,
        "peer_id": peer_id,
        "type": "typing",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{VK_API_URL}/messages.setActivity", data=params)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"VK API error: {data['error']}")


async def send_message(peer_id: int, text: str, random_id: int | None = None) -> None:
    """Отправить сообщение клиенту.

    `random_id` — защита от дубликата на стороне ВК: с тем же значением он
    повторную отправку отбрасывает. Для ответов в диалоге он случайный (два
    одинаковых ответа подряд — законный случай), а для сообщений, которые
    бот отправляет сам по событию, вызывающий передаёт значение, выведенное
    из этого события.
    """
    params = {
        "access_token": settings.vk_access_token,
        "v": settings.vk_api_version,
        "peer_id": peer_id,
        "message": text,
        "random_id": random.getrandbits(31) if random_id is None else random_id,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{VK_API_URL}/messages.send", data=params)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"VK API error: {data['error']}")
