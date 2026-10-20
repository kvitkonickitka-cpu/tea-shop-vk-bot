import logging
import random
import time
import re

import httpx

from app.core.config import settings

VK_API_URL = "https://api.vk.com/method"

logger = logging.getLogger(__name__)

_resolved: dict[str, int] = {}

# Коды ВК «сообщение не доставить»: клиент запретил сообщения сообщества,
# закрыл их настройками приватности или занёс сообщество в чёрный список.
UNREACHABLE_CODES = {900, 901, 902}


class VkApiError(RuntimeError):
    """Ошибка API ВК с кодом — по коду видно, можно ли писать клиенту вообще."""

    def __init__(self, error):
        super().__init__(f"VK API error: {error}")
        self.code = error.get("error_code") if isinstance(error, dict) else None


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


async def send_message(
    peer_id: int, text: str, random_id: int | None = None, keyboard: str | None = None
) -> None:
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
    if keyboard:
        # Inline-клавиатура JSON-строкой; её собирает app/messages/keyboard.py.
        params["keyboard"] = keyboard
    if any(url and url in text for url in (settings.conditions_url, settings.pd_consent_url)):
        # Ссылки на условия и на согласие ВК разворачивал в карточку статьи во
        # весь экран: сводка со счётом становилась вдвое длиннее, а просьба о
        # согласии терялась за картинкой. Ссылка остаётся ссылкой, карточки
        # нет — только в сообщениях с этими ссылками.
        params["dont_parse_links"] = 1
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{VK_API_URL}/messages.send", data=params)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise VkApiError(data["error"])
    # Только время удачной отправки — для разбора задержек. Сбои ВК в журнал
    # Ops не идут: «нельзя писать этому клиенту» (901) — не поломка.
    from app.modules.ops import journal

    journal.note_call("vk", "отправка сообщения", time.monotonic() - started)


async def call(method: str, **params) -> dict:
    """Метод ВК API токеном сообщества. Ошибка ВК — VkApiError."""
    data = {"access_token": settings.vk_access_token, "v": settings.vk_api_version, **params}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"{VK_API_URL}/{method}", data=data)
        response.raise_for_status()
        body = response.json()
    if "error" in body:
        raise VkApiError(body["error"])
    return body.get("response")


async def token_permissions() -> list[str]:
    """Права токена сообщества (groups.getTokenPermissions) — только чтение."""
    result = await call("groups.getTokenPermissions") or {}
    return [str(p.get("name")) for p in result.get("permissions") or []]


async def wall_post(message: str) -> int:
    """Запись на стене сообщества от его имени. Только по кнопке менеджера (отзывы)."""
    group = "".join(ch for ch in (settings.vk_group_id or "") if ch.isdigit())
    result = await call("wall.post", owner_id=f"-{group}", from_group=1, message=message)
    return int((result or {}).get("post_id") or 0)


async def first_name(user_id: int) -> str:
    """Имя пользователя ВК — для подписи отзыва «с именем из ВК» (только при публикации)."""
    result = await call("users.get", user_ids=str(user_id)) or []
    return str(result[0].get("first_name") or "") if result else ""
