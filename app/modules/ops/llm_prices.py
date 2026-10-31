"""Цены LLM API за миллион токенов, долларов — для учёта расхода (`llm_usage`).

OpenAI: стандартные цены на 31.10.2026; Anthropic оставлен для истории.
Новую модель в `OPENAI_MODEL` или `OPS_ASSISTANT_MODEL` нужно дописать сюда,
иначе её запросы попадут в учёт без стоимости (токены останутся).

Запись в кэш на 5 минут стоит 1,25 цены входа, чтение из кэша — 0,1 (бот
ставит метку кэша без `ttl`, то есть на 5 минут). Размышление модели
оплачивается как выход и уже входит в `output_tokens`.
"""

from __future__ import annotations

from decimal import Decimal

# модель → (вход, выход, запись в кэш на 5 минут, чтение из кэша), $ за 1M токенов
PRICES: dict[str, tuple[str, str, str, str]] = {
    "gpt-6-luna": ("0.10", "0.50", "0.125", "0.01"),
    "gpt-6.1-sol": ("2", "10", "2.50", "0.10"),
    "claude-sonnet-5-5": ("2", "10", "2.50", "0.20"),
    "claude-sonnet-5": ("2", "10", "2.50", "0.20"),
}

_MILLION = Decimal(1_000_000)


def _price(model: str) -> tuple[Decimal, ...] | None:
    """Цена по id модели; id с датой или суффиксом — по самому длинному совпавшему началу."""
    for name in sorted(PRICES, key=len, reverse=True):
        if model == name or model.startswith(name + "-") or model.startswith(name + "@"):
            return tuple(Decimal(p) for p in PRICES[name])
    return None


def cost_usd(model: str, *, input_tokens: int, output_tokens: int,
             cache_read_tokens: int, cache_write_tokens: int) -> Decimal | None:
    price = _price(model or "")
    if price is None:
        return None
    inp, out, write, read = price
    total = (input_tokens * inp + output_tokens * out + cache_write_tokens * write
             + cache_read_tokens * read) / _MILLION
    return total.quantize(Decimal("0.000001"))
