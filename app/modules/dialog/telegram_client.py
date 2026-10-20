import json
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from app.core.config import settings

TELEGRAM_API_URL = "https://api.telegram.org"

# Сколько ждать ответа Telegram. Две секунды — только внутри вебхука ВК: VK
# отводит на него около восьми секунд, и недоступный Telegram не должен
# съедать весь бюджет, оставляя без ответа и клиента. Везде, где секундомера
# нет (тик расписания, очередь, проверка связи), ждём дольше: через прокси
# Telegram отвечает и за две с лишним секунды, и сообщение, которое на деле
# ушло, считалось неотправленным — а тик досылал его вторым экземпляром.
_TIMEOUT_SECONDS = 10
_HURRY_TIMEOUT_SECONDS = 2

_hurry: ContextVar[bool] = ContextVar("telegram_hurry", default=False)


@contextmanager
def hurry():
    """Внутри блока Telegram ждём коротко: идёт обработка вебхука ВК."""
    token = _hurry.set(True)
    try:
        yield
    finally:
        _hurry.reset(token)


class TelegramUnavailable(RuntimeError):
    """Telegram не ответил вовсе: сеть, прокси, таймаут.

    Отдельный класс нужен досылке: если Telegram молчит, остальные записи
    очереди в этот тик пробовать бессмысленно — только время тика тратить.
    """


async def send_message(text: str, chat_id: str | None = None) -> None:
    # По умолчанию — чат менеджера, куда идут эскалации. Отдельным адресатом
    # пользуются мини-отчёты по завершённым диалогам: им нужен свой чат, чтобы
    # не тонуть в срочных сообщениях и не топить их.
    base_url = settings.telegram_api_base_url or TELEGRAM_API_URL
    url = f"{base_url}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id or settings.telegram_manager_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    # Секрет нужен только когда идём через свой прокси (не напрямую в Telegram) —
    # без него прокси стал бы открытым релеем: им мог бы пользоваться кто угодно
    # со своим токеном, а не только наш бот.
    headers = (
        {"X-Proxy-Secret": settings.telegram_proxy_secret}
        if settings.telegram_api_base_url and settings.telegram_proxy_secret
        else {}
    )

    # Токен бота лежит прямо в пути URL, а httpx кладёт URL в текст своих
    # ошибок. Без этой обёртки любой сбой Telegram писал бы токен в логи —
    # и в облачные, и в ответы служебных эндпоинтов. Поэтому наружу отдаём
    # только код и тело ответа, без адреса.
    timeout = _HURRY_TIMEOUT_SECONDS if _hurry.get() else _TIMEOUT_SECONDS
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(
                f"Telegram ответил {error.response.status_code}: {error.response.text[:300]}"
            ) from None
        except httpx.RequestError as error:
            raise TelegramUnavailable(f"Telegram недоступен: {type(error).__name__}") from None

        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")


class TelegramConflict(RuntimeError):
    """409 на getUpdates: обновления этого бота уже забирает кто-то другой.

    Бывает в секунды деплоя, когда старый контейнер ещё жив, или если на
    токен поставили вебхук. Опросу надо подождать, а не сыпать ошибками.
    """


async def call(method: str, payload: dict | None = None, *, timeout: float = _TIMEOUT_SECONDS):
    """Любой метод Bot API через тот же путь, что и send_message: прокси и секрет.

    Ошибки — без адреса запроса: в нём токен бота (см. send_message).
    Возвращает поле `result` ответа.
    """
    base_url = settings.telegram_api_base_url or TELEGRAM_API_URL
    url = f"{base_url}/bot{settings.telegram_bot_token}/{method}"
    headers = (
        {"X-Proxy-Secret": settings.telegram_proxy_secret}
        if settings.telegram_api_base_url and settings.telegram_proxy_secret
        else {}
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(url, json=payload or {}, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409:
                raise TelegramConflict(error.response.text[:300]) from None
            raise RuntimeError(
                f"Telegram ответил {error.response.status_code}: {error.response.text[:300]}"
            ) from None
        except httpx.RequestError as error:
            raise TelegramUnavailable(f"Telegram недоступен: {type(error).__name__}") from None
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {str(data)[:300]}")
    return data.get("result")


async def reply(text: str, *, chat_id: str, reply_to: int) -> dict:
    """Ответ на конкретное сообщение (HTML). Возвращает отправленное сообщение."""
    return await call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        # Сообщение могли удалить, пока шёл ответ, — тогда ответ уходит просто в чат.
        "reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True},
    })


async def typing(chat_id: str) -> None:
    """«Печатает…» в чате — живёт около пяти секунд."""
    await call("sendChatAction", {"chat_id": chat_id, "action": "typing"})


async def get_me() -> dict:
    return await call("getMe")


async def get_updates(offset: int | None, *, wait_seconds: int) -> list[dict]:
    """Длинный опрос: Telegram держит запрос до wait_seconds, пока нет нового."""
    # callback_query — кнопки под карточками (публикация отзыва, C7).
    payload: dict = {"timeout": wait_seconds, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return await call("getUpdates", payload, timeout=wait_seconds + 15)


async def send_document(filename: str, content: bytes, *, chat_id: str, caption: str = "",
                        reply_to: int | None = None) -> None:
    """Файл в чат (выгрузка КУДиР и подобное) — тем же путём через прокси.

    multipart, а не JSON: файл идёт телом запроса. Ошибки — без адреса (в нём токен).
    """
    base_url = settings.telegram_api_base_url or TELEGRAM_API_URL
    url = f"{base_url}/bot{settings.telegram_bot_token}/sendDocument"
    headers = (
        {"X-Proxy-Secret": settings.telegram_proxy_secret}
        if settings.telegram_api_base_url and settings.telegram_proxy_secret
        else {}
    )
    data = {"chat_id": chat_id, "parse_mode": "HTML"}
    if caption:
        data["caption"] = caption[:1000]
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(url, data=data, files={"document": (filename, content)}, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(
                f"Telegram ответил {error.response.status_code}: {error.response.text[:300]}"
            ) from None
        except httpx.RequestError as error:
            raise TelegramUnavailable(f"Telegram недоступен: {type(error).__name__}") from None


async def download_file(file_id: str, *, limit_bytes: int = 2_000_000) -> bytes:
    """Файл из сообщения (документ в чате Ops) — getFile и скачивание тем же путём через прокси."""
    info = await call("getFile", {"file_id": file_id})
    if int(info.get("file_size") or 0) > limit_bytes:
        raise ValueError("файл больше 2 МБ")
    base_url = settings.telegram_api_base_url or TELEGRAM_API_URL
    url = f"{base_url}/file/bot{settings.telegram_bot_token}/{info['file_path']}"
    headers = (
        {"X-Proxy-Secret": settings.telegram_proxy_secret}
        if settings.telegram_api_base_url and settings.telegram_proxy_secret
        else {}
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(f"Telegram ответил {error.response.status_code} на скачивание файла") from None
        except httpx.RequestError as error:
            raise TelegramUnavailable(f"Telegram недоступен: {type(error).__name__}") from None
    return response.content
