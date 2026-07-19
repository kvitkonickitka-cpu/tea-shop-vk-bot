from anthropic import AsyncAnthropic

from app.core.config import settings

SYSTEM_PROMPT = (
    "Ты — консультант чайного магазина в сообществе ВКонтакте. "
    "Отвечай дружелюбно и по делу на вопросы об ассортименте, заказах, "
    "доставке и оплате. Если не знаешь точного ответа (например, актуальные "
    "цены или наличие товара), честно скажи, что уточнишь у менеджера."
)

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def generate_reply(user_message: str) -> str:
    response = await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
