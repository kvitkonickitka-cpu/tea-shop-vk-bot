"""Какие методы витрины ВК доступны нашим токенам.

`market.get` групповым токеном не работает: ВК отвечает «27 Group
authorization failed: method is unavailable with group auth». Это не про
права — право `market` у токена сообщества есть, иначе не читались бы
заказы, — а про то, что витрину группе смотреть не разрешают вовсе.

Скрипт перебирает методы и показывает, кто из них с каким токеном
отвечает. Гонять его можно с ноутбука: ВК API живёт в интернете, база и
контейнер здесь не нужны.

    python scripts/vk_market_probe.py

Токены берутся из `.env` рядом с репозиторием:

- `VK_ACCESS_TOKEN` — токен сообщества, тот, которым работает бот;
- `VK_USER_TOKEN` — необязательный токен администратора сообщества.
  Если витрину отдают только пользовательскому токену, это и будет
  ответом на вопрос, чем её читать.

Ни один токен скрипт не печатает и никуда не отправляет, кроме самого ВК.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
VK_API_URL = "https://api.vk.com/method"
API_VERSION = "5.199"


def env_value(name: str) -> str:
    """Значение из .env: последнее присваивание, без кавычек."""
    if not ENV_FILE.exists():
        return ""
    value = ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{name}="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
    return value


def owner_id(group_id: str) -> int:
    digits = "".join(ch for ch in group_id if ch.isdigit())
    if not digits:
        raise SystemExit("В .env нет VK_GROUP_ID — непонятно, чью витрину смотреть.")
    return -int(digits)


async def call(client: httpx.AsyncClient, method: str, token: str, **params) -> tuple[bool, str]:
    """Позвать метод. Возвращает «получилось» и короткое описание ответа."""
    try:
        response = await client.get(
            f"{VK_API_URL}/{method}",
            params={**params, "access_token": token, "v": API_VERSION},
        )
        data = response.json()
    except Exception as error:  # сеть, а не ВК
        return False, f"не дозвонились: {type(error).__name__}"

    if "error" in data:
        error = data["error"]
        return False, f"{error.get('error_code')} {error.get('error_msg')}"

    payload = data.get("response")
    if isinstance(payload, dict):
        if "count" in payload:
            items = payload.get("items") or []
            return True, f"count={payload['count']}, получено {len(items)}"
        return True, "ответил"
    if isinstance(payload, list):
        return True, f"элементов {len(payload)}"
    return True, str(payload)[:60]


async def probe(label: str, token: str, group: int) -> None:
    print(f"\n=== {label} ===")
    if not token:
        print("  токена нет в .env — пропускаем")
        return

    checks = (
        ("groups.getById", {"group_id": abs(group)}),
        ("market.get", {"owner_id": group, "count": 3, "extended": 1}),
        ("market.getAlbums", {"owner_id": group, "count": 3}),
        ("market.getCategories", {"count": 3}),
        ("market.search", {"owner_id": group, "count": 3}),
    )

    async with httpx.AsyncClient(timeout=15) as client:
        for method, params in checks:
            ok, note = await call(client, method, token, **params)
            print(f"  {'✔' if ok else '✘'} {method:22} {note}")


async def main() -> None:
    group_id = env_value("VK_GROUP_ID")
    group = owner_id(group_id)
    print(f"Витрина: owner_id={group}")

    await probe("токен сообщества (VK_ACCESS_TOKEN)", env_value("VK_ACCESS_TOKEN"), group)
    await probe("токен администратора (VK_USER_TOKEN)", env_value("VK_USER_TOKEN"), group)

    print(
        "\nЧто это значит:\n"
        "  27 Group authorization failed — метод не работает с токеном\n"
        "  сообщества в принципе, права тут ни при чём.\n"
        "  15 Access denied — метод доступен, но не этому токену.\n"
        "  5 User authorization failed — токен просрочен или отозван."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
