"""Ozon Delivery API: расчёт доставки и заказы.

Отличия от СДЭКа, из-за которых код не похож на `cdek_client`:

- токен берётся на **другом хосте** (`xapi.ozon.ru`), а методы живут на
  `api-delivery.ozon.ru`;
- `scope` в запросе токена — массив уровней доступа, а не строка;
- перед API стоит защита от DDoS (`testcookie`): первый запрос получает
  редирект с `Set-Cookie`, и его надо повторить с этой кукой. Куку храним
  и переиспользуем, иначе редирект будет на каждый вызов;
- цена считается не «калькулятором», а методом `/v1/order/checkout`, и он
  требует **код пункта выдачи** — одного города, как у СДЭКа, мало.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Уровни доступа, которые запрашиваем у токена. Должны совпадать с теми,
# что выданы приложению в кабинете, иначе Ozon откажет.
SCOPES = [
    "delivery-api.order",
    "delivery-api.shipment-method",
    "delivery-api.delivery-point",
    "delivery-api.dropoff-point",
    "delivery-api.posting",
]

_TIMEOUT_SECONDS = 10
_MAX_REDIRECTS = 3
# Проверено живым запросом: Ozon отвечает «размер страницы должен быть от 1
# до 100», хотя в спецификации ограничения нет.
_MAX_PAGE = 100

_token: str | None = None
_token_expires_at: float = 0.0
# Кука от защиты Ozon живёт между запросами: без неё каждый вызов начинался
# бы с редиректа, то есть стоил бы вдвое дороже.
_cookies = httpx.Cookies()


class OzonError(RuntimeError):
    """Ошибка на стороне Ozon: отказ авторизации, отказ расчёта, недоступность."""


@dataclass(frozen=True)
class ShipmentMethod:
    id: int
    name: str
    status: str


@dataclass(frozen=True)
class DeliveryPoint:
    id: int
    name: str
    address: str
    shipment_method_ids: tuple[int, ...]
    # Закрытые пункты Ozon из каталога не убирает, помечает флагом. Предложить
    # клиенту закрытый пункт — значит отправить его к запертой двери.
    is_active: bool = True
    # pvz или postamat: в постамат посылку кладут в ячейку, и клиенту это
    # стоит сказать заранее.
    kind: str = ""


@dataclass(frozen=True)
class Quote:
    """Во что обойдётся доставка. Страховка приходит отдельной строкой."""

    delivery_cost: float
    insurance_cost: float
    days: int

    @property
    def total(self) -> float:
        # Клиенту показываем сумму: Ozon выставит нам обе строки, и «забыть»
        # страховку значит повторить историю с НДС у СДЭКа.
        return round(self.delivery_cost + self.insurance_cost, 2)


def is_configured() -> bool:
    return bool(settings.ozon_client_id and settings.ozon_client_secret)


def _describe_failure(response: httpx.Response) -> str:
    parts = [f"HTTP {response.status_code}"]
    try:
        data = response.json()
    except ValueError:
        return f"{parts[0]}: {response.text[:300]}"

    error = data.get("error") or {}
    if isinstance(error, dict) and error:
        parts.append(f"{error.get('code', '?')}: {error.get('message', '')}")
    elif error:
        parts.append(str(error))
    else:
        parts.append(str(data)[:300])
    return "; ".join(parts)


async def _post(client: httpx.AsyncClient, url: str, payload: dict, headers: dict) -> httpx.Response:
    """POST с ручной обработкой редиректа от защиты Ozon.

    Автоматическое следование за редиректом не подходит: httpx на 302
    превращает POST в GET и теряет тело, а Ozon ждёт тот же запрос по новому
    адресу. Поэтому повторяем сами, забрав куку.
    """
    for attempt in range(_MAX_REDIRECTS):
        response = await client.post(url, json=payload, headers=headers, cookies=_cookies)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response

        _cookies.update(response.cookies)
        url = str(response.headers.get("location") or url)
        logger.info("Ozon перенаправил запрос (попытка %s)", attempt + 1)

    raise OzonError("Ozon зациклил редиректы — не удалось получить ответ")


async def _get_token(client: httpx.AsyncClient) -> str:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at:
        return _token

    response = await _post(
        client,
        settings.ozon_auth_url,
        {
            "client_id": settings.ozon_client_id,
            "client_secret": settings.ozon_client_secret,
            "grant_type": "client_credentials",
            "scope": SCOPES,
        },
        {"Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        raise OzonError(f"Ozon не выдал токен — {_describe_failure(response)}")

    data = response.json()
    token = data.get("access_token")
    if not token:
        raise OzonError(f"В ответе Ozon нет токена: {str(data)[:300]}")

    _token = token
    # Минута запаса, чтобы не попасть в гонку с истечением прямо в запросе.
    _token_expires_at = time.time() + int(data.get("expires_in", 3600)) - 60
    return _token


async def call(path: str, payload: dict) -> dict:
    """Вызов метода Ozon Delivery API с авторизацией."""
    if not is_configured():
        raise OzonError("OZON_CLIENT_ID/OZON_CLIENT_SECRET не заданы")

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        token = await _get_token(client)
        response = await _post(
            client,
            f"{settings.ozon_api_base_url}{path}",
            payload,
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    if response.status_code >= 400:
        raise OzonError(f"Ozon отказал на {path} — {_describe_failure(response)}")
    return response.json()


async def shipment_methods() -> list[ShipmentMethod]:
    """Методы доставки магазина. Их идентификатор обязателен для расчёта."""
    data = await call("/v1/shipment-method/search", {"pagination": {"limit": 100}})
    return [
        ShipmentMethod(
            id=item.get("shipment_method_id") or item.get("id"),
            name=item.get("name", ""),
            status=item.get("status", ""),
        )
        for item in data.get("shipment_methods") or data.get("result") or []
    ]


def viewport_around(latitude: float, longitude: float, span: float = 0.15) -> dict:
    """Прямоугольник вокруг точки. Полградуса широты — примерно 55 км."""
    return {
        "left_bottom": {"latitude": latitude - span, "longitude": longitude - span},
        "right_top": {"latitude": latitude + span, "longitude": longitude + span},
    }


async def dropoff_points(
    viewport: dict, address_search: str = "", limit: int = 10
) -> list[dict]:
    """Пункты отгрузки — куда мы сами сдаём посылки.

    `viewport` обязателен, хотя в спецификации помечен необязательным:
    сервер отвечает «missing required properties including: viewport».
    Поиск по строке адреса работает только внутри этого прямоугольника.
    """
    filters: dict = {"is_bulky": False, "viewport": viewport}
    if address_search:
        filters["address_search"] = address_search

    data = await call(
        "/v1/dropoff-point/search",
        {"filters": filters, "pagination": {"limit": min(limit, _MAX_PAGE)}},
    )
    return data.get("dropoff_points") or []


async def delivery_point_ids(cursor: str = "", limit: int = _MAX_PAGE) -> tuple[list[dict], str]:
    """Страница списка пунктов выдачи: только идентификаторы.

    Адресов здесь нет и фильтра по городу тоже — Ozon отдаёт весь каталог
    постранично. Подробности приходится добирать методом `info`.
    """
    pagination: dict = {"limit": min(limit, _MAX_PAGE)}
    if cursor:
        pagination["cursor"] = cursor
    data = await call("/v1/delivery-point/list", {"pagination": pagination})
    return data.get("delivery_points") or [], data.get("next_cursor") or ""


async def delivery_points_info(ids: list[int]) -> list[DeliveryPoint]:
    """Подробности пунктов выдачи по их идентификаторам."""
    data = await call("/v1/delivery-point/info", {"delivery_point_ids": ids})
    points = []
    for item in data.get("delivery_points") or []:
        points.append(
            DeliveryPoint(
                id=item.get("delivery_point_id"),
                name=item.get("name", ""),
                address=item.get("full_address", ""),
                shipment_method_ids=tuple(item.get("shipment_method_ids") or ()),
                is_active=item.get("is_active", True),
                kind=item.get("type", ""),
            )
        )
    return points


def _declared_value(amount: float) -> dict:
    # Сумма у Ozon — строка, а не число: так в спецификации, и на число он
    # отвечает отказом разбора.
    return {"amount": str(int(amount)), "currency_code": "RUB"}


def _dimensions(weight_grams: int, length_mm: int, width_mm: int, height_mm: int) -> dict:
    return {
        "weight_g": weight_grams,
        "length_mm": length_mm,
        "width_mm": width_mm,
        "height_mm": height_mm,
    }


async def available_points(
    *,
    delivery_point_ids: list[int],
    shipment_method_id: int,
    weight_grams: int,
    length_mm: int,
    width_mm: int,
    height_mm: int,
    declared_value: float,
) -> set[int]:
    """Какие из пунктов примут именно нашу посылку.

    Каталог у себя мы держим общий на всю страну, а метод доставки у нас свой
    и обслуживает не каждый пункт. Без этой проверки бот предложил бы клиенту
    пункт, на котором потом сорвётся расчёт, — а выбор уже сделан.
    """
    if not delivery_point_ids:
        return set()

    data = await call(
        "/v1/delivery-point/check-availability",
        {
            "delivery_point_ids": delivery_point_ids,
            "shipment_method_id": shipment_method_id,
            "postings": [
                {
                    "request_id": 1,
                    "declared_value": _declared_value(declared_value),
                    "dimensions": _dimensions(weight_grams, length_mm, width_mm, height_mm),
                }
            ],
        },
    )

    allowed = set()
    for result in data.get("results") or []:
        point_id = result.get("delivery_point_id")
        if point_id and not result.get("error"):
            allowed.add(int(point_id))
    return allowed


async def checkout(
    *,
    shipment_method_id: int,
    delivery_point_id: int,
    phone_number: str,
    weight_grams: int,
    length_mm: int,
    width_mm: int,
    height_mm: int,
    declared_value: float,
) -> Quote:
    """Предварительный расчёт: сколько будет стоить и сколько идти.

    Габариты обязательны — в отличие от СДЭКа, одним весом не обойтись.
    Пункт выдачи тоже: цену «до города», как у калькулятора СДЭКа, Ozon не
    считает, поэтому сначала выбираем пункт, а уже потом называем цену.
    """
    payload = {
        "recipient": {"phone_number": phone_number},
        "delivery": {"delivery_point": {"delivery_point_id": delivery_point_id}},
        "postings": [
            {
                "request_id": 1,
                "shipment_method_id": shipment_method_id,
                "declared_value": _declared_value(declared_value),
                "dimensions": _dimensions(weight_grams, length_mm, width_mm, height_mm),
            }
        ],
    }
    data = await call("/v1/order/checkout", payload)

    results = data.get("results") or []
    if not results:
        raise OzonError(f"Ozon не вернул расчёт: {str(data)[:300]}")

    result = results[0]
    if result.get("error"):
        error = result["error"]
        raise OzonError(f"Ozon отказал в расчёте — {error.get('code', '?')}: {error.get('message', '')}")

    posting = result.get("posting") or {}
    return Quote(
        delivery_cost=_money(posting.get("estimated_delivery_cost")),
        insurance_cost=_money(posting.get("estimated_insurance_cost")),
        days=int(posting.get("estimated_delivery_days") or 0),
    )


def _money(value) -> float:
    """Суммы у Ozon приходят объектом с amount, а amount — строкой."""
    if value is None:
        return 0.0
    if isinstance(value, dict):
        value = value.get("amount", 0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
