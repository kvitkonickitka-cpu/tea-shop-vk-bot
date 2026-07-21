import httpx

from app.core.config import settings

TELEGRAM_API_URL = "https://api.telegram.org"


async def send_message(text: str) -> None:
    url = f"{TELEGRAM_API_URL}/bot{settings.telegram_bot_token}/sendMessage"
    params = {"chat_id": settings.telegram_manager_chat_id, "text": text}

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, data=params)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
