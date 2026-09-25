import logging
import random
import re

import httpx

from app.core.config import settings

VK_API_URL = "https://api.vk.com/method"

logger = logging.getLogger(__name__)

_resolved: dict[str, int] = {}


def _screen_name(raw: str) -> str:
    """Короткое имя из того, что вписали: ссылки, «@имя», «id123»."""
    text = (raw or "").strip()
    text = re.sub(r"^(https?://)?(m\.)?vk\.(com|ru)/", "", text)
    return text.lstrip("@").strip("/ ")


async def resolve_user_id(raw) -> int | None:
    """Числовой id пользователя ВК по тому, что записано в настройке.

    Принимает число, «id123», короткое имя и ссылку на страницу. Короткое
    имя переводится через `utils.resolveScreenName` — один раз за жизнь
    процесса. Не вышло — None и строка в логе, а не падение.
    """
    name = _screen_name(str(raw or ""))
    if not name or name == "0":
        return None
    if name.isdigit():
        return int(name)
    if re.fullmatch(r"id\d+", name):
        return int(name[2:])
    if name in _resolved:
        return _resolved[name]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{VK_API_URL}/utils.resolveScreenName",
                data={
                    "access_token": settings.vk_access_token,
                    "v": settings.vk_api_version,
                    "screen_name": name,
                },
            )
            data = response.json()
    except Exception as error:
        logger.error("Не перевели «%s» в id ВК: %s", name, error)
        return None
    found = data.get("response") or {}
    if found.get("type") != "user" or not found.get("object_id"):
        logger.error("«%s» — не страница пользователя ВК: %s", name, str(data)[:200])
        return None
    _resolved[name] = int(found["object_id"])
    return _resolved[name]


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
