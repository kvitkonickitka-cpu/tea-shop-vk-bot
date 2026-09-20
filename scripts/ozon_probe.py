#!/usr/bin/env python3
"""Разведка по Ozon Доставке: что настроено и во что обойдётся каталог.

Тот же приём, что сработал со СДЭКом: сначала узнаём недостающие числа
живым запросом, потом пишем код, который на них опирается.

    python scripts/ozon_probe.py "Краснодар, Игнатова"

Аргумент — по какому адресу искать пункт отгрузки (куда мы сдаём посылки).
Ключи берутся из .env или переменных окружения, в аргументах не передаются.
Секретов в выводе нет.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.modules.delivery import ozon_client  # noqa: E402


async def show_methods() -> int | None:
    print("== Методы доставки ==")
    try:
        methods = await ozon_client.shipment_methods()
    except ozon_client.OzonError as error:
        print(f"  не получилось: {error}")
        return None

    if not methods:
        print("  ни одного метода не нашлось — добавь его в кабинете Ozon")
        return None

    for method in methods:
        print(f"  id={method.id}  {method.name}  статус: {method.status}")

    active = [m for m in methods if m.status == "active"]
    if len(active) == 1:
        print(f"\n  → OZON_SHIPMENT_METHOD_ID={active[0].id}")
        return active[0].id
    if active:
        print("\n  активных методов больше одного — выбери нужный и впиши его id")
        return active[0].id
    print("\n  активных методов нет: проверь статус метода в кабинете")
    return None


async def show_dropoff_points(query: str) -> None:
    print(f"\n== Пункты отгрузки по запросу «{query}» ==")
    try:
        points = await ozon_client.dropoff_points(query)
    except ozon_client.OzonError as error:
        print(f"  не получилось: {error}")
        return

    if not points:
        print("  ничего не нашлось — попробуй другой запрос")
        return
    for point in points[:10]:
        address = point.get("full_address") or point.get("address") or "—"
        print(f"  id={point.get('dropoff_point_id') or point.get('id')}  {address}")


async def measure_catalog() -> None:
    print("\n== Каталог пунктов выдачи ==")
    print("  (фильтра по городу у Ozon нет — каталог отдаётся постранично)")

    started = time.monotonic()
    try:
        page, cursor = await ozon_client.delivery_point_ids(limit=1000)
    except ozon_client.OzonError as error:
        print(f"  не получилось: {error}")
        return
    first_page = time.monotonic() - started

    print(f"  первая страница: {len(page)} пунктов за {first_page:.2f}с")
    print(f"  курсор дальше: {'есть' if cursor else 'нет — каталог кончился'}")
    if not page:
        return

    ids = [p.get("delivery_point_id") for p in page[:20] if p.get("delivery_point_id")]
    started = time.monotonic()
    try:
        details = await ozon_client.delivery_points_info(ids)
    except ozon_client.OzonError as error:
        print(f"  подробности не получились: {error}")
        return

    print(f"  подробности по {len(ids)} пунктам за {time.monotonic() - started:.2f}с")
    for point in details[:3]:
        methods = list(point.shipment_method_ids)[:3]
        print(f"    {point.id}  {point.address}  методы: {methods}")

    if cursor:
        print("\n  Каталог больше одной страницы: искать пункт по требованию")
        print("  не выйдет, придётся выгружать его к себе и обновлять по таймеру.")


async def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else (settings.cdek_from_address or "Краснодар")

    if not ozon_client.is_configured():
        print("OZON_CLIENT_ID/OZON_CLIENT_SECRET не заданы — добавь их в .env")
        return 2

    print(f"Хост API: {settings.ozon_api_base_url}")
    print(f"Токен с:  {settings.ozon_auth_url}")
    print(f"Уровни:   {', '.join(ozon_client.SCOPES)}\n")

    method_id = await show_methods()
    await show_dropoff_points(query)
    await measure_catalog()

    print("\nЧто дальше: пришли этот вывод — по нему станет понятно,")
    print("какой id метода прописывать и как устроен каталог пунктов.")
    return 0 if method_id else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
