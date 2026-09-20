#!/usr/bin/env python3
"""Замер: сколько весит и сколько занимает список пунктов выдачи СДЭК.

Бот отвечает клиенту внутри 8-секундного окна VK, поэтому прежде чем
встраивать выбор пункта выдачи в диалог, надо знать цену вопроса: сколько
пунктов в городе, сколько весит ответ и за сколько он приходит. Скрипт
ничего не меняет, только измеряет.

    python scripts/cdek_points_probe.py Москва Краснодар

Города можно перечислить через пробел, по умолчанию — Москва.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.modules.delivery import cdek_client  # noqa: E402


async def probe(client: httpx.AsyncClient, token: str, city: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}

    started = time.monotonic()
    response = await client.get(
        f"{settings.cdek_api_base_url}/v2/location/cities",
        headers=headers,
        params={"city": city, "country_codes": "RU", "size": 5},
    )
    cities_time = time.monotonic() - started

    if response.status_code >= 400:
        print(f"{city}: не нашли город — HTTP {response.status_code} {response.text[:200]}")
        return

    cities = response.json()
    if not cities:
        print(f"{city}: СДЭК не знает такого города")
        return

    city_code = cities[0]["code"]
    print(f"{city}: код города {city_code}, поиск города {cities_time:.2f}с")

    started = time.monotonic()
    response = await client.get(
        f"{settings.cdek_api_base_url}/v2/deliverypoints",
        headers=headers,
        params={"city_code": city_code, "type": "PVZ", "country_code": "RU"},
    )
    points_time = time.monotonic() - started

    if response.status_code >= 400:
        print(f"{city}: список пунктов не отдался — HTTP {response.status_code} {response.text[:200]}")
        return

    size_kb = len(response.content) / 1024
    points = response.json()
    print(f"{city}: пунктов {len(points)}, ответ {size_kb:.0f} КБ, время {points_time:.2f}с")

    for point in points[:3]:
        location = point.get("location", {})
        print(
            f"    [{point.get('code')}] {location.get('address_full') or location.get('address')}"
            f"   часы: {point.get('work_time') or '—'}"
        )
    if len(points) > 3:
        print(f"    ... и ещё {len(points) - 3}")
    print()


async def main() -> int:
    cities = sys.argv[1:] or ["Москва"]
    contour = "ПЕСОЧНИЦА" if "edu" in settings.cdek_api_base_url else "БОЕВОЙ КОНТУР"
    print(f"Контур: {contour} ({settings.cdek_api_base_url})\n")

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            token = await cdek_client._get_access_token(client)
        except cdek_client.CdekError as error:
            print(f"Не получилось: {error}")
            return 1

        for city in cities:
            await probe(client, token, city)

    print("Что смотреть: если ответ приходит дольше ~2с или весит десятки мегабайт,")
    print("в путь обработки сообщения это встраивать нельзя — нужен кэш или очередь.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
