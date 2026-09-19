import httpx

from app.core.config import settings

TELEGRAM_API_URL = "https://api.telegram.org"

# VK отводит на весь вебхук около восьми секунд, а обращение к Telegram — лишь
# одна из операций в этом пути. Прежние 10 секунд означали, что недоступный
# Telegram в одиночку съедал весь бюджет и без ответа оставался не только
# менеджер, но и клиент.
_TIMEOUT_SECONDS = 2


async def send_message(text: str) -> None:
    base_url = settings.telegram_api_base_url or TELEGRAM_API_URL
    url = f"{base_url}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_manager_chat_id,
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

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
