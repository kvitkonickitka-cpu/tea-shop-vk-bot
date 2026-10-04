"""Геопозиция клиента вместо адреса с карты: ближайшие пункты — кодом, без модели.

Раньше в большом городе бот просил улицу и дом пункта, адрес с карты или
скриншот. Скриншот уходил в Claude как есть — с чужими метками на карте и
всем, что попало в кадр. Теперь там, где бот спрашивает, где забрать, стоит
кнопка [📍 Отправить геопозицию] (`GEO_LOCATION_ENABLED`, если приложение
клиента её умеет), и пришедшую точку разбирает код:

- пункты выбранного перевозчика (до выбора — Ozon) в радиусе
  `GEO_SEARCH_RADIUS_KM`, ближние первыми, до четырёх — с расстоянием,
  ценой и датой, кнопками; список ложится в черновик, как при поиске по
  улице, и дальше выбор идёт обычным путём;
- у Ozon координаты пунктов — в своей копии каталога; пока их там нет,
  геопозиция для Ozon не предлагается. У СДЭКа координаты приходят в списке
  пунктов города, поэтому для него нужен город.

Геопозиция — персональные данные: по точке видно, где человек живёт или
работает. Координаты превращаются в метку [GEO_n] ещё до записи сообщения
в базу (`inbound.accept`), хранятся зашифрованными в `pii_vault` не дольше
`GEO_RETENTION_HOURS` и не попадают ни в историю, которую видит модель, ни
в логи, журнал воронки и карточки менеджеру. Модель видит «[GEO_1] клиент
отправил геопозицию» и ответ бота с адресами пунктов — они публичные.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.messages import funnel, keyboard as keyboards, templates
from app.modules.orders import state

logger = logging.getLogger(__name__)

# Подпись нажатия в истории диалога — без координат, только метка.
HISTORY_NOTE = "[{label}] Клиент отправил геопозицию"


def coordinates_of(message: dict) -> tuple[float, float] | None:
    """Координаты из message_new: `geo.coordinates` (кнопка location и «поделиться местом»)."""
    geo = message.get("geo") if isinstance(message.get("geo"), dict) else None
    raw = (geo or {}).get("coordinates") or {}
    try:
        lat, lon = float(raw["latitude"]), float(raw["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def city_of(message: dict) -> str:
    """Город, который ВК приложил к точке (`geo.place.city`), — если есть."""
    place = ((message.get("geo") or {}).get("place") or {}) if isinstance(message.get("geo"), dict) else {}
    city = place.get("city") if isinstance(place, dict) else ""
    return str(city or "").strip()


async def hide(peer_id: int, message: dict) -> dict:
    """Сообщение с геопозицией — в базу уже с меткой вместо координат.

    Без ключа шифрования метку не завести — тогда координаты выбрасываем:
    лучше попросить улицу, чем хранить точку открытым текстом.
    """
    found = coordinates_of(message)
    if found is None:
        return message
    from app import privacy

    label = None
    try:
        label = await privacy.geo_label(peer_id, *found)
    except Exception:
        logger.exception("Не завели метку геопозиции для peer_id=%s", peer_id)
    hidden = dict(message)
    hidden["geo"] = {"label": label, "city": city_of(message)}
    return hidden


def is_geo(message: dict) -> bool:
    geo = message.get("geo")
    return isinstance(geo, dict) and ("label" in geo or "coordinates" in geo)


async def offer_for(peer_id: int, method: str | None = None) -> bool:
    """Ставить ли кнопку геопозиции: флаг, приложение клиента, координаты пунктов."""
    if not settings.geo_location_enabled:
        return False
    try:
        if not await keyboards.shows_location_button(peer_id):
            return False
        if method in ("cdek_pvz",):
            return True
        if method not in (None, "", "ozon_pvz"):
            return False
        from app.modules.delivery import ozon_catalog

        return await ozon_catalog.has_coordinates()
    except Exception:
        logger.exception("Не решили, ставить ли кнопку геопозиции peer_id=%s", peer_id)
        return False


def button(version) -> dict:
    return keyboards.location_button({"a": "geo", "v": version})


def has_button(keyboard: dict | None) -> bool:
    return any(button["action"]["type"] == "location"
               for row in (keyboard or {}).get("buttons", []) for button in row)


async def note_shown(peer_id: int, keyboard: dict | None, where: str) -> None:
    """Кнопка геопозиции ушла клиенту — в журнал воронки, без координат."""
    if not has_button(keyboard):
        return
    try:
        if await keyboards.for_peer(peer_id, keyboard) is None:
            return
        await funnel.record(peer_id, "geo_button_shown", where=where)
    except Exception:
        logger.exception("Не записали показ кнопки геопозиции peer_id=%s", peer_id)


def distance_text(meters: float | None) -> str:
    """«≈ 600 м», «≈ 1,2 км» — расстояние по прямой, округлённо."""
    if meters is None:
        return ""
    if meters < 1000:
        return f"≈ {max(100, int(round(meters / 100.0)) * 100)} м"
    km = round(meters / 1000.0, 1)
    return f"≈ {km:g} км".replace(".", ",")


async def handle(peer_id: int, message: dict) -> tuple[str, dict | None, str] | None:
    """Геопозиция пришла: ответ клиенту, клавиатура и запись для истории — или None.

    None — черновика нет: геопозицию некуда приложить, отвечает модель (она
    увидит только метку).
    """
    from app import privacy
    from app.modules.orders import buttons, conversation, eta, upgrade

    geo = message.get("geo") or {}
    label = geo.get("label")
    found = await privacy.geo_value(peer_id, label) if label else None
    note = HISTORY_NOTE.format(label=label or "GEO")
    draft = await state.get_draft(peer_id)
    if draft is None or draft.stage not in ("awaiting_delivery", "awaiting_confirmation"):
        return None

    method = draft.delivery_method if draft.delivery_method in ("ozon_pvz", "cdek_pvz") else "ozon_pvz"
    carrier = "Ozon" if method == "ozon_pvz" else "СДЭК"
    city = (draft.details.get("address") or draft.details.get("storefront_city") or geo.get("city") or "").strip()
    if found is None:
        await funnel.record(peer_id, "geo_sent", carrier=upgrade.carrier_of(method), found=0, lost=True)
        return templates.geo_lost(), None, note
    if method == "cdek_pvz" and not city:
        await funnel.record(peer_id, "geo_sent", carrier="cdek", found=0)
        return templates.geo_need_city(carrier), None, note

    result = await conversation._execute_set_delivery_method(
        peer_id, {"method": method, "address": city or templates.GEO_CITY_PLACEHOLDER, "_near": found},
    )
    fresh = await state.get_draft(peer_id)
    listed = list((fresh.details.get("shown_points") if fresh else None) or [])
    if result.tool_result == conversation.GEO_NOTHING_NEAR or not listed or fresh.delivery_method != method:
        await funnel.record(peer_id, "geo_sent", carrier=upgrade.carrier_of(method), found=0)
        return templates.geo_nothing_near(carrier, settings.geo_search_radius_km), None, note

    await funnel.record(peer_id, "geo_sent", carrier=upgrade.carrier_of(method), found=len(listed))
    keyboard, hint = await buttons.for_reply(peer_id)
    shows = await keyboards.for_peer(peer_id, keyboard) is not None
    text = templates.geo_points(
        carrier=carrier, shown=listed,
        per_point_prices=all(point.get("price") is not None for point in listed),
        delivery_cost=fresh.delivery_cost, when=eta.receive(fresh.details.get(eta.KEY)),
        surcharge=bool(fresh.details.get(upgrade.SURCHARGE)),
        ask=await _ask(peer_id, fresh, shows), hint=hint if shows else "",
        distances=[distance_text(point.get("distance")) for point in listed],
    )
    return text, keyboard, note


async def _ask(peer_id: int, draft, button: bool) -> str:
    """Что попросить вместе со списком: получатель уже есть, из заказа, прошлый или никакого."""
    details = draft.details
    if details.get("recipient_name") and details.get("recipient_email"):
        return templates.ASK_POINT
    candidate = details.get("storefront_recipient")
    if candidate:
        return templates.storefront_ask_email(candidate["name"], candidate["phone"])
    from app.modules.orders import repeat_delivery

    last = await repeat_delivery.last_recipient_for(peer_id)
    if last is not None and last.email:
        return templates.storefront_ask_last(last.name, last.phone, last.email, button=button)
    return templates.ASK_POINT_AND_RECIPIENT
