from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Приложение 15 спецификации: истинный режим заказа. Первое слово — как
# посылка попадает от нас в СДЭК, второе — как она попадает к клиенту.
#
# Различать их обязательно. Самый дешёвый тариф почти всегда «склад-склад»,
# то есть клиент едет за посылкой сам. Если показать эту цену как стоимость
# доставки по адресу, магазин будет недобирать на каждом заказе.
MODE_DOOR_DOOR = 1
MODE_DOOR_WAREHOUSE = 2
MODE_WAREHOUSE_DOOR = 3
MODE_WAREHOUSE_WAREHOUSE = 4
MODE_DOOR_POSTAMAT = 6
MODE_WAREHOUSE_POSTAMAT = 7

# Курьер привозит клиенту по адресу.
TO_DOOR = (MODE_DOOR_DOOR, MODE_WAREHOUSE_DOOR)
# Клиент забирает сам в пункте выдачи.
TO_PICKUP = (MODE_DOOR_WAREHOUSE, MODE_WAREHOUSE_WAREHOUSE)
# Клиент забирает сам из постамата.
TO_POSTAMAT = (MODE_DOOR_POSTAMAT, MODE_WAREHOUSE_POSTAMAT)

_ORDER_TYPE_ONLINE_SHOP = 1
# Таймаут заведомо меньше 8 секунд, которые VK даёт на весь вебхук: иначе
# один медленный ответ СДЭКа съедает всё окно и клиент не получает ничего.
# Лучше остаться без цены и сказать об этом, чем промолчать.
_TIMEOUT_SECONDS = 3.5
# Регистрации даём больше: оборванный на полпути POST /v2/orders оставляет
# заказ в неизвестном состоянии, а это хуже, чем задержка.
_ORDER_TIMEOUT_SECONDS = 5

# Тариф по одному и тому же городу и весу не меняется в пределах разговора,
# а поход за ним стоит двух запросов. Память контейнера переживает диалог,
# этого достаточно.
_TARIFFS_TTL_SECONDS = 600
_tariffs_cache: dict[tuple, tuple[float, list]] = {}

_token: str | None = None
_token_expires_at: float = 0.0


class CdekError(RuntimeError):
    """Ошибка на стороне СДЭК: отказ в авторизации, отказ расчёта, недоступность."""


@dataclass(frozen=True)
class Tariff:
    code: int
    name: str
    delivery_sum: float
    period_min: int
    period_max: int
    delivery_mode: int

    @property
    def period(self) -> str:
        if self.period_min == self.period_max:
            return f"{self.period_min} раб. дн."
        return f"{self.period_min}–{self.period_max} раб. дн."


def _describe_failure(response: httpx.Response) -> str:
    # СДЭК отвечает и кодом, и списком ошибок в теле, причём одно без другого
    # встречается. Собираем всё, что есть: иначе в логах остаётся голое «нет
    # тарифов», по которому причину не восстановить.
    parts = [f"HTTP {response.status_code}"]
    try:
        data = response.json()
    except ValueError:
        return f"{parts[0]}: {response.text[:300]}"

    for error in data.get("errors") or []:
        parts.append(f"{error.get('code', '?')}: {error.get('message', '')}")
    if len(parts) == 1:
        parts.append(str(data)[:300])
    return "; ".join(parts)


async def _get_access_token(client: httpx.AsyncClient) -> str:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at:
        return _token

    response = await client.post(
        f"{settings.cdek_api_base_url}/v2/oauth/token",
        params={
            "grant_type": "client_credentials",
            "client_id": settings.cdek_client_id,
            "client_secret": settings.cdek_client_secret,
        },
    )
    if response.status_code >= 400:
        raise CdekError(f"СДЭК не выдал токен — {_describe_failure(response)}")

    data = response.json()
    if "access_token" not in data:
        raise CdekError(f"В ответе СДЭК нет токена: {str(data)[:300]}")

    _token = data["access_token"]
    # Минута запаса, чтобы не попасть в гонку с истечением прямо в запросе.
    _token_expires_at = time.time() + data.get("expires_in", 3600) - 60
    return _token


async def calculate_tariffs(
    to_address: str,
    weight_grams: int,
    *,
    delivery_point: str | None = None,
) -> list[Tariff]:
    """Все доступные тарифы до указанного адреса.

    Фильтровать по режиму доставки — задача вызывающего: см. `cheapest`.
    """
    if not settings.cdek_from_address:
        raise CdekError("CDEK_FROM_ADDRESS не задан — расчёт невозможен")

    cache_key = (to_address.strip().lower(), weight_grams, delivery_point)
    cached = _tariffs_cache.get(cache_key)
    if cached and time.time() - cached[0] < _TARIFFS_TTL_SECONDS:
        return cached[1]

    payload: dict = {
        "type": _ORDER_TYPE_ONLINE_SHOP,
        "lang": "rus",
        "from_location": {"address": settings.cdek_from_address},
        "to_location": {"address": to_address},
        "packages": [{"weight": weight_grams}],
    }
    if delivery_point:
        # Код ПВЗ повышает точность расчёта сроков для режимов «до склада».
        payload["delivery_point"] = delivery_point

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        token = await _get_access_token(client)
        response = await client.post(
            f"{settings.cdek_api_base_url}/v2/calculator/tarifflist",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    if response.status_code >= 400:
        raise CdekError(f"Расчёт СДЭК не удался — {_describe_failure(response)}")

    data = response.json()
    if data.get("errors"):
        raise CdekError(f"Расчёт СДЭК не удался — {_describe_failure(response)}")
    for warning in data.get("warnings") or []:
        logger.warning("СДЭК предупреждает: %s", warning)

    tariffs = [
        Tariff(
            code=t["tariff_code"],
            name=t["tariff_name"],
            delivery_sum=float(t["delivery_sum"]),
            period_min=t["period_min"],
            period_max=t["period_max"],
            delivery_mode=t["delivery_mode"],
        )
        for t in data.get("tariff_codes") or []
    ]
    if not tariffs:
        raise CdekError(f"СДЭК не вернул ни одного тарифа — {_describe_failure(response)}")

    _tariffs_cache[cache_key] = (time.time(), tariffs)
    return tariffs


def cheapest(tariffs: list[Tariff], modes: tuple[int, ...]) -> Tariff | None:
    """Самый дешёвый тариф среди нужных режимов доставки."""
    suitable = [t for t in tariffs if t.delivery_mode in modes]
    return min(suitable, key=lambda t: t.delivery_sum) if suitable else None


@dataclass(frozen=True)
class DeliveryPoint:
    code: str
    address: str
    work_time: str

    def describe(self) -> str:
        hours = f" ({self.work_time})" if self.work_time else ""
        return f"{self.address}{hours}"


# Список пунктов по городу живёт в памяти контейнера: в Москве их тысячи, и
# тянуть их на каждое сообщение — верный способ выпасть из 8 секунд, которые
# даёт VK. Час жизни выбран как компромисс: пункты открываются и закрываются
# не каждый день, а контейнер всё равно переживает не дольше.
_POINTS_TTL_SECONDS = 3600
_points_cache: dict[str, tuple[float, list[DeliveryPoint]]] = {}

_NOISE_WORDS = (
    "улица", "ул", "дом", "д", "проспект", "пр", "пр-т", "проезд",
    "переулок", "пер", "шоссе", "ш", "бульвар", "б-р", "строение", "стр",
    "корпус", "корп", "к", "офис", "оф", "помещение", "пом",
)


def _normalize(text: str) -> list[str]:
    """Значимые слова адреса: без пунктуации и без «ул.», «д.» и прочего шума."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return [w for w in cleaned.split() if w and w not in _NOISE_WORDS]


async def _city_code(client: httpx.AsyncClient, token: str, city: str) -> int | None:
    response = await client.get(
        f"{settings.cdek_api_base_url}/v2/location/cities",
        headers={"Authorization": f"Bearer {token}"},
        params={"city": city, "country_codes": "RU", "size": 1},
    )
    if response.status_code >= 400:
        raise CdekError(f"Не нашли город «{city}» — {_describe_failure(response)}")

    cities = response.json()
    # Ждём список городов. Если пришло что-то другое — лучше понятная
    # ошибка, чем KeyError из середины разбора.
    if not isinstance(cities, list):
        raise CdekError(f"СДЭК ответил на поиск города не списком: {str(cities)[:200]}")
    if not cities:
        return None
    return cities[0].get("code")


async def city_points(city: str) -> list[DeliveryPoint]:
    """Пункты выдачи города — из кэша, если он ещё свежий."""
    cached = _points_cache.get(city.lower())
    if cached and time.time() - cached[0] < _POINTS_TTL_SECONDS:
        return cached[1]

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        token = await _get_access_token(client)
        code = await _city_code(client, token, city)
        if code is None:
            raise CdekError(f"СДЭК не знает города «{city}»")

        response = await client.get(
            f"{settings.cdek_api_base_url}/v2/deliverypoints",
            headers={"Authorization": f"Bearer {token}"},
            params={"city_code": code, "type": "PVZ", "country_code": "RU"},
        )

    if response.status_code >= 400:
        raise CdekError(f"Список пунктов не отдался — {_describe_failure(response)}")

    points = [
        DeliveryPoint(
            code=p["code"],
            address=(p.get("location") or {}).get("address_full")
            or (p.get("location") or {}).get("address")
            or "",
            work_time=p.get("work_time") or "",
        )
        for p in response.json()
    ]
    _points_cache[city.lower()] = (time.time(), points)
    return points


def match_points(points: list[DeliveryPoint], hint: str, limit: int = 3) -> list[DeliveryPoint]:
    """Пункты, подходящие под то, что назвал клиент, — лучшие сверху.

    Клиент пишет адрес как придётся («тверская 12», «на Тверской»), поэтому
    сравниваем по значимым словам, а не по строке целиком.
    """
    wanted = _normalize(hint)
    if not wanted:
        return []

    scored = []
    for point in points:
        words = set(_normalize(point.address))
        hits = sum(1 for w in wanted if w in words)
        if hits:
            # Точное попадание всех слов важнее, чем длина адреса.
            scored.append((hits, -len(point.address), point))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0][0] if scored else 0
    return [point for hits, _, point in scored if hits == best][:limit]


async def find_delivery_point(city: str, hint: str, limit: int = 3) -> list[DeliveryPoint]:
    return match_points(await city_points(city), hint, limit)


@dataclass(frozen=True)
class RegisteredOrder:
    uuid: str
    number: str


def _order_items(items: list[dict], weight_per_item: int) -> list[dict]:
    """Позиции для СДЭКа: заказу типа «интернет-магазин» они обязательны."""
    result = []
    for index, item in enumerate(items, start=1):
        price = float(item.get("price", 0))
        result.append(
            {
                "name": item.get("name", "Товар"),
                # Артикула в каталоге нет, поэтому подставляем порядковый
                # номер: СДЭК требует непустой ware_key, но не проверяет его.
                "ware_key": str(index),
                "payment": {"value": 0},
                "cost": price,
                "amount": int(item.get("quantity", 1)),
                "weight": weight_per_item,
            }
        )
    return result


async def register_order(
    *,
    number: str,
    tariff_code: int,
    recipient_name: str,
    recipient_phone: str,
    items: list[dict],
    weight_grams: int,
    to_address: str | None = None,
    delivery_point: str | None = None,
    comment: str = "",
) -> RegisteredOrder:
    """Завести заказ в СДЭКе.

    Заказ попадает в личный кабинет и ждёт там, пока посылку реально не
    сдадут в отделение: до этого его можно удалить. Отдельного состояния
    «черновик» у СДЭКа нет, ближайшее к нему — вот это ожидание.
    """
    if not settings.cdek_shipment_point:
        raise CdekError("CDEK_SHIPMENT_POINT не задан — СДЭК не примет заказ от склада")
    if not delivery_point and not to_address:
        raise CdekError("Не знаем, куда везти: нет ни кода пункта, ни адреса")

    weight_per_item = max(1, weight_grams // max(1, sum(int(i.get("quantity", 1)) for i in items)))

    payload: dict = {
        "type": _ORDER_TYPE_ONLINE_SHOP,
        "number": number,
        "tariff_code": tariff_code,
        # Посылку отвозим в отделение сами, поэтому все наши тарифы —
        # «от склада», а для них СДЭК требует код отделения отправки.
        "shipment_point": settings.cdek_shipment_point,
        "recipient": {
            "name": recipient_name,
            "phones": [{"number": recipient_phone}],
        },
        "packages": [
            {
                "number": number,
                "weight": weight_grams,
                "items": _order_items(items, weight_per_item),
            }
        ],
    }
    if comment:
        payload["comment"] = comment
    if delivery_point:
        payload["delivery_point"] = delivery_point
    else:
        payload["to_location"] = {"address": to_address}

    async with httpx.AsyncClient(timeout=_ORDER_TIMEOUT_SECONDS) as client:
        token = await _get_access_token(client)
        response = await client.post(
            f"{settings.cdek_api_base_url}/v2/orders",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    if response.status_code >= 400:
        raise CdekError(f"СДЭК не принял заказ — {_describe_failure(response)}")

    data = response.json()
    uuid = (data.get("entity") or {}).get("uuid")
    if not uuid:
        raise CdekError(f"В ответе СДЭК нет идентификатора заказа: {str(data)[:300]}")

    # СДЭК принимает заказ асинхронно: 202 означает «взяли в обработку», а не
    # «завели». Ошибки валидации всплывут позже, при запросе состояния.
    for request in data.get("requests") or []:
        for error in request.get("errors") or []:
            raise CdekError(f"СДЭК отклонил заказ — {error.get('code')}: {error.get('message')}")

    return RegisteredOrder(uuid=uuid, number=number)


async def order_state(uuid: str) -> dict:
    """Что стало с заказом: СДЭК обрабатывает регистрацию асинхронно."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        token = await _get_access_token(client)
        response = await client.get(
            f"{settings.cdek_api_base_url}/v2/orders/{uuid}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise CdekError(f"Не узнали состояние заказа — {_describe_failure(response)}")
    return response.json()
