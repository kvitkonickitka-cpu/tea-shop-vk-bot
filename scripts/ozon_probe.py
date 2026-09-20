#!/usr/bin/env python3
"""Разведка по Ozon Доставке: что настроено и во что обойдётся каталог.

Тот же приём, что сработал со СДЭКом: сначала узнаём недостающие числа
живым запросом, потом пишем код, который на них опирается.

    python scripts/ozon_probe.py                      # Краснодар
    python scripts/ozon_probe.py 45.035 38.975        # своя точка
    python scripts/ozon_probe.py 45.035 38.975 Игнатова
    python scripts/ozon_probe.py --count            # пересчитать весь каталог

Аргументы — координаты центра поиска и, необязательно, часть адреса.
Пункты отгрузки Ozon ищет внутри прямоугольника на карте, а не по городу,
поэтому нужны именно координаты.
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


async def show_dropoff_points(latitude: float, longitude: float, query: str) -> None:
    where = f" по запросу «{query}»" if query else ""
    print(f"\n== Пункты отгрузки вокруг {latitude}, {longitude}{where} ==")
    viewport = ozon_client.viewport_around(latitude, longitude)
    try:
        points = await ozon_client.dropoff_points(viewport, query)
    except ozon_client.OzonError as error:
        print(f"  не получилось: {error}")
        return

    if not points:
        print("  ничего не нашлось — расширь область или убери часть адреса")
        return
    for point in points[:10]:
        address = point.get("full_address") or "—"
        приёмка = "самоприёмка" if point.get("has_self_acceptance") else "без самоприёмки"
        print(f"  id={point.get('dropoff_point_id')}  {point.get('name', '')}"
              f"  {address}  ({приёмка})")


async def measure_catalog() -> None:
    print("\n== Каталог пунктов выдачи ==")
    print("  (фильтра по городу у Ozon нет — каталог отдаётся постранично)")

    started = time.monotonic()
    try:
        page, cursor = await ozon_client.delivery_point_ids()
    except ozon_client.OzonError as error:
        print(f"  не получилось: {error}")
        return
    first_page = time.monotonic() - started

    print(f"  первая страница: {len(page)} пунктов за {first_page:.2f}с"
          f"  (больше 100 за раз Ozon не отдаёт)")
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


async def count_catalog() -> None:
    """Пройти каталог до конца и сказать, сколько в нём пунктов.

    Нужно, чтобы решить, как строить поиск пункта для клиента: выгружать
    каталог к себе или как-то иначе. Занимает несколько минут.
    """
    print("== Полный пересчёт каталога ==")
    print("  идём постранично до конца, это небыстро\n")

    total = 0
    pages = 0
    cursor = ""
    started = time.monotonic()

    while True:
        try:
            page, cursor = await ozon_client.delivery_point_ids(cursor=cursor)
        except ozon_client.OzonError as error:
            print(f"  оборвалось на странице {pages + 1}: {error}")
            break

        total += len(page)
        pages += 1
        if pages % 20 == 0:
            print(f"  {pages} страниц, {total} пунктов, {time.monotonic() - started:.0f}с")
        if not cursor or not page:
            break

    elapsed = time.monotonic() - started
    print(f"\n  всего: {total} пунктов на {pages} страницах за {elapsed:.0f}с")

    # Сколько идентификаторов принимает info за раз — от этого зависит,
    # сколько будет стоить выгрузка адресов.
    print("\n== Сколько адресов за один запрос ==")
    page, _ = await ozon_client.delivery_point_ids()
    ids = [p.get("delivery_point_id") for p in page if p.get("delivery_point_id")]
    for size in (20, 50, 100):
        if len(ids) < size:
            break
        started = time.monotonic()
        try:
            details = await ozon_client.delivery_points_info(ids[:size])
        except ozon_client.OzonError as error:
            print(f"  по {size}: отказ — {error}")
            continue
        print(f"  по {size}: вернулось {len(details)} за {time.monotonic() - started:.2f}с")

    if total:
        print(f"\n  Прикидка выгрузки адресов пачками по 100: "
              f"{total // 100 + 1} запросов")


async def main() -> int:
    # Центр Краснодара: оттуда мы отправляем посылки.
    latitude = float(sys.argv[1]) if len(sys.argv) > 1 else 45.035
    longitude = float(sys.argv[2]) if len(sys.argv) > 2 else 38.975
    query = sys.argv[3] if len(sys.argv) > 3 else ""

    if not ozon_client.is_configured():
        print("OZON_CLIENT_ID/OZON_CLIENT_SECRET не заданы — добавь их в .env")
        return 2

    if "--count" in sys.argv:
        await count_catalog()
        return 0

    print(f"Хост API: {settings.ozon_api_base_url}")
    print(f"Токен с:  {settings.ozon_auth_url}")
    print(f"Уровни:   {', '.join(ozon_client.SCOPES)}\n")

    method_id = await show_methods()
    await show_dropoff_points(latitude, longitude, query)
    await measure_catalog()

    print("\nЧто дальше: пришли этот вывод — по нему станет понятно,")
    print("какой id метода прописывать и как устроен каталог пунктов.")
    return 0 if method_id else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
