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
