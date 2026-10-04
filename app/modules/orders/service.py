"""Заказ, оформленный в витрине сообщества, минуя диалог.

ВК присылает `market_order_new`, когда клиент нажал «Оформить» в товарах
сообщества. Дальше заказ ведёт тот же путь, что и заказ из переписки:
выбор доставки инструментами, живой расчёт у перевозчика, счёт ЮKassa,
отправление после оплаты.

Раньше эта ветка жила отдельной жизнью: считала СДЭК сама, называла цену
тарифа без НДС и сборов (ту самую, из-за которой 320 руб расчёта
превращались в 397 руб счёта), не знала про Ozon и не умела выставить
счёт. Клиент из витрины и клиент из переписки получали разные магазины.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.messages import client as client_messages, manager as manager_messages, templates
from app.modules.dialog import vk_client
from app.modules.orders import address as address_parser, contacts, order_chat, state, vk_orders_client
from app.modules.orders.state import OrderDraft

logger = logging.getLogger(__name__)


def _rubles(value: Any) -> float:
    """Сумма ВК в рублях: в API она приходит копейками и строкой."""
    if isinstance(value, dict):
        value = value.get("amount", 0)
    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


def _items_of(raw_items: list[dict]) -> list[dict]:
    """Состав заказа в том виде, в каком его держит черновик.

    Схему ВК разбираем защитно: товар без названия или цены пропускаем, но
    из-за него не теряем весь заказ.
    """
    items = []
    for row in raw_items or []:
        product = row.get("item") or {}
        name = (product.get("title") or "").strip()
        if not name:
            continue
        quantity = int(row.get("quantity") or 1)
        price = _rubles(product.get("price"))
        if not price:
            # Цена за единицу неизвестна — берём из строки заказа целиком.
            price = round(_rubles(row.get("price")) / max(quantity, 1), 2)
        items.append({"name": name, "quantity": quantity, "price": price})
    return items


def _address_of(order: dict) -> str:
    """Адрес доставки из заказа ВК — в объекте `delivery`, у старых схем — в корне."""
    delivery = order.get("delivery") if isinstance(order.get("delivery"), dict) else {}
    address = delivery.get("address") or order.get("delivery_address") or order.get("address")
    if isinstance(address, dict):
        address = address.get("address") or address.get("text")
    return str(address or "").strip()


def _recipient_of(order: dict) -> tuple[str, str] | None:
    """Получатель из заказа ВК (`recipient.name`, `recipient.phone`) — если телефон настоящий."""
    raw = order.get("recipient") if isinstance(order.get("recipient"), dict) else {}
    name = str(raw.get("name") or "").strip()
    phone = contacts.normalize_phone(str(raw.get("phone") or ""))
    return (name, phone) if name and phone else None


async def _direct_points(
    user_id: int, order_id, order: dict, items: list[dict], items_total: float, address: str
) -> bool:
    """Сразу пункты Ozon рядом с адресом из заказа. False — прежний вопрос «в какой город».

    Улицы в адресе нет, а пунктов в городе много — вместо списка просим
    адрес пункта (улица и дом, адрес с карты Ozon или скриншот).

    Тот же поиск, что и «город и улица» в диалоге: до четырёх пунктов с
    номерами, первыми — на этой улице, с ценой у каждого, если Ozon ответил
    вовремя; список ложится в черновик, и выбор «1», кнопкой или адресом идёт
    обычным путём до автоматического счёта.
    """
    from app.messages import keyboard as keyboards
    from app.modules.orders import buttons, conversation, repeat_delivery

    if not settings.storefront_direct_points_enabled:
        return False
    parsed = address_parser.city_and_street(address)
    if parsed is None:
        return False
    city, street = parsed
    try:
        await conversation._execute_set_delivery_method(
            user_id, {"method": "ozon_pvz", "address": city, "pickup_point": street}
        )
    except Exception:
        logger.exception("Витринный заказ %s: пункты рядом с адресом не подобрали", order_id)
        return False
    draft = await state.get_draft(user_id)
    shown = list((draft.details.get("shown_points") if draft else None) or [])
    # Улицы в адресе нет, а город большой — пункты не перечисляем, просим
    # адрес пункта, как и в диалоге.
    asked = bool(draft and draft.delivery_method == "ozon_pvz" and draft.details.get("point_asked"))
    if draft is None or not (shown or asked) or draft.details.get("ozon_point_id"):
        return False

    keyboard, hint = await buttons.for_reply(user_id)
    shows_buttons = await keyboards.for_peer(user_id, keyboard) is not None
    last = await repeat_delivery.last_recipient_for(user_id)
    candidate = _recipient_of(order)
    if last is not None and last.email:
        ask = (
            templates.ask_last_recipient(last.name, last.phone, last.email, button=shows_buttons)
            if asked else
            templates.storefront_ask_last(last.name, last.phone, last.email, button=shows_buttons)
        )
    elif candidate is not None:
        draft.details["storefront_recipient"] = {"name": candidate[0], "phone": candidate[1]}
        await state.set_draft(user_id, draft)
        ask = (templates.storefront_with_point_email if asked else templates.storefront_ask_email)(*candidate)
    else:
        ask = templates.STOREFRONT_WITH_POINT_ALL if asked else templates.STOREFRONT_ASK_ALL
    if asked:
        text = templates.storefront_ask_point(
            order_id=order_id, items=items, items_total=items_total, city=city,
            delivery_cost=draft.delivery_cost, ask=ask,
        )
    else:
        text = templates.storefront_points(
            order_id=order_id, items=items, items_total=items_total, shown=shown,
            per_point_prices=all(point.get("price") is not None for point in shown),
            delivery_cost=draft.delivery_cost, ask=ask, hint=hint if shows_buttons else "",
        )
    await client_messages.send(
        peer_id=user_id, ref=f"vk_order:{order_id}", event_type=templates.STOREFRONT_ORDER,
        text=text, keyboard=keyboard,
    )
    return True


async def _tell_manager(order_id: int, user_id: int, text: str) -> None:
    await manager_messages.notify(
        manager_messages.STOREFRONT_ORDER,
        f"🛒 <b>Заказ из витрины №{order_id}</b>\n{text}\n\n"
        f"{vk_client.dialog_link(user_id)}",
        peer_id=user_id,
        chat_id=order_chat.chat_id(),
    )


# Сколько ждать сообщения группе с корзиной, если в событии состава нет.
_UNPARSED_WAIT_SECONDS = 5


async def handle_new_order(order_event: dict[str, Any]) -> None:
    """Событие `market_order_new`: заказ из раздела «Товары»."""
    order_id = order_event.get("id") or order_event.get("order_id")
    if order_id is None:
        logger.error("market_order_new без номера заказа, ключи: %s", sorted(order_event.keys()))
        return
    user_id = order_event.get("user_id")
    # Сам объект события — уже заказ ВК. Полный запрос — чтобы не зависеть
    # от того, что ВК положил в событие; не ответил — работаем с событием.
    order = order_event
    try:
        order = await vk_orders_client.get_order(order_id, user_id=user_id) or order_event
    except Exception:
        logger.exception("Заказ витрины %s: market.getOrderById не ответил, берём данные события", order_id)

    # Сырой объект в лог не пишем: в нём имя, телефон и адрес покупателя.
    # Для разбора схемы хватает ключей — по ним видно, где лежат адрес и
    # получатель.
    logger.info(
        "Заказ витрины %s: поля %s, delivery %s, recipient %s",
        order_id, sorted(order.keys()),
        sorted((order.get("delivery") or {}).keys()) if isinstance(order.get("delivery"), dict) else "—",
        sorted((order.get("recipient") or {}).keys()) if isinstance(order.get("recipient"), dict) else "—",
    )
    user_id = order.get("user_id") or user_id
    if not user_id:
        logger.error("Заказ витрины %s без user_id — некому писать", order_id)
        return

    try:
        raw_items = await vk_orders_client.get_order_items(order_id, user_id=user_id)
    except Exception:
        logger.exception("Не получили состав витринного заказа %s", order_id)
        raw_items = order.get("preview_order_items") or []
    items = _items_of(raw_items)
    if not items and not await client_messages.already_sent(f"vk_order:{order_id}", "storefront_started"):
        # Событие — главный источник: если и в нём состава нет, заказ не
        # теряем. Сообщение группе с корзиной может прийти следом — тогда
        # заказ заведёт оно; иначе ведёт менеджер.
        await asyncio.sleep(_UNPARSED_WAIT_SECONDS)
        if not await client_messages.claim_once(f"vk_order:{order_id}", "storefront_started", int(user_id)):
            return
        logger.error("Витринный заказ %s: состав не разобрали, зовём менеджера", order_id)
        await _tell_manager(order_id, int(user_id), "Состав заказа не разобрали — оформить доставку руками.")
        await vk_client.send_message(
            int(user_id), f"Заказ №{order_id} принят! Проверим состав и напишем здесь расчёт доставки 🙏",
        )
        return
    await start_order(
        int(user_id), int(order_id), items=items, address=_address_of(order),
        recipient=_recipient_of(order), source="market_order_new",
    )


# --- служебные сообщения ВК о заказе ------------------------------------------
#
# Когда клиент оформляет заказ в «Товарах», ВК сам пишет в диалог с ним два
# сообщения от имени сообщества: клиенту — «Ваш заказ № N оформлен», группе
# (клиент его не видит) — «Новый заказ N» с корзиной, адресом и телефоном.
# К нам они приходят как `message_reply` без автора-администратора. Раньше
# бот их выбрасывал, и модель видела только шаблонный вопрос клиента «как
# оплатить заказ?» — без заказа. Теперь они — второй источник заказа, на
# случай если `market_order_new` не пришёл или ВК не отдал заказ по API.

_CLIENT_NOTICE = re.compile(r"^\s*Ваш заказ\s*№\s*(\d+)\s+оформлен", re.IGNORECASE)
_ADMIN_NOTICE = re.compile(r"^\s*Новый заказ\s+№?\s*(\d+)", re.IGNORECASE)
_ORDER_LINK = re.compile(r"orders(\d+)_(\d+)")
_BUYER_LINK = re.compile(r"[?&]sel=(\d+)")
_CART_LINE = re.compile(r"^(.+?)\s*\((\d+)\)\s*([\d\s\u00a0\u202f]+)\s*(?:₽|руб)")


def _number(text: str) -> float | None:
    digits = re.sub(r"[^\d,.]", "", text or "").replace(",", ".")
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _field(text: str, title: str) -> str:
    match = re.search(rf"^\s*{title}\s*:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""


@dataclass
class Notice:
    order_id: int
    kind: str  # client — видит клиент, admin — только группа
    user_id: int | None
    items: list[dict]
    address: str
    recipient: tuple[str, str] | None


def parse_notice(text: str, attachments: list | None = None) -> Notice | None:
    """Служебное сообщение ВК о заказе — или None, если это не оно."""
    text = text or ""
    client = _CLIENT_NOTICE.search(text)
    admin = None if client else _ADMIN_NOTICE.search(text)
    if not (client or admin):
        return None
    order_id = int((client or admin).group(1))
    user_id = None
    link = _ORDER_LINK.search(text) or _BUYER_LINK.search(text)
    if link:
        user_id = int(link.group(1))

    items: list[dict] = []
    if admin:
        cart = text.split("Корзина:", 1)[1] if "Корзина:" in text else ""
        for line in cart.strip().splitlines():
            if not line.strip():
                break
            row = _CART_LINE.match(line.strip())
            if row:
                quantity = int(row.group(2))
                line_total = _number(row.group(3)) or 0
                items.append({"name": row.group(1).strip(), "quantity": quantity,
                              "price": round(line_total / max(quantity, 1), 2)})
        address = _field(text, "Адрес доставки")
        name, phone = _field(text, "Получатель"), contacts.normalize_phone(_field(text, "Контактный телефон"))
        recipient = (name, phone) if name and phone else None
    else:
        # Клиенту ВК пишет только город и товар карточкой-вложением.
        delivery = re.search(r"^\s*Доставка[^:\n]*:\s*(.+?)\s*$", text, re.MULTILINE)
        address = delivery.group(1) if delivery else ""
        recipient = None
        for attachment in attachments or []:
            product = (attachment or {}).get("market") if (attachment or {}).get("type") == "market" else None
            if product and product.get("title"):
                items.append({"name": product["title"].strip(), "quantity": 1,
                              "price": _rubles(product.get("price"))})
    return Notice(order_id, "client" if client else "admin", user_id, items, address.strip(), recipient)


async def handle_notice(peer_id: int, message: dict) -> bool:
    """Сообщение от имени группы без автора-админа: если это заказ — завести его."""
    notice = parse_notice(message.get("text", ""), message.get("attachments"))
    if notice is None:
        return False
    buyer = notice.user_id or peer_id
    logger.info("Уведомление ВК о заказе %s (%s) для peer_id=%s: товаров %d",
                notice.order_id, notice.kind, buyer, len(notice.items))
    await start_order(buyer, notice.order_id, items=notice.items, address=notice.address,
                      recipient=notice.recipient, source=f"notice_{notice.kind}")
    return True


# --- заказ в диалоге ------------------------------------------------------------


def _catalog_item(name: str, price: float, quantity: int) -> dict:
    """Позиция с названием из каталога, если оно узнаётся: по нему GTIN и чек.

    В ВК товар называется «Дянь Хун // 100 грамм», в таблице — «Дянь Хун
    100 г». Цена остаётся та, с которой клиент оформил заказ.
    """
    from app.modules.catalog import service as catalog_service

    variants = [name, re.sub(r"\s*//\s*", " ", name)]
    variants.append(re.sub(r"\bграмм\w*", "г", variants[-1]))
    for variant in variants:
        found = catalog_service.find_item(" ".join(variant.split()))
        if found is not None:
            return {"name": found["name"], "quantity": quantity, "price": price}
    return {"name": name, "quantity": quantity, "price": price}


async def start_order(
    peer_id: int, order_id: int, *, items: list[dict], address: str,
    recipient: tuple[str, str] | None, source: str,
) -> None:
    """Завести заказ из «Товаров» один раз — из того источника, что пришёл первым.

    Без состава не начинаем: его принесёт следующий источник (событие или
    сообщение группе с корзиной). Отметка в журнале отправок — до работы:
    три источника приходят почти одновременно, а заказ должен быть один.
    """
    if not items:
        logger.info("Заказ витрины %s (%s): состава нет, ждём другой источник", order_id, source)
        return
    if not await client_messages.claim_once(f"vk_order:{order_id}", "storefront_started", peer_id):
        logger.info("Заказ витрины %s уже заведён, источник %s не нужен", order_id, source)
        return

    from app.messages import funnel
    from app.modules.analytics import service as analytics
    from app.modules.dialog import history as dialog_history

    items = [_catalog_item(item["name"], item["price"], item["quantity"]) for item in items]
    items_total = round(sum(item["price"] * item["quantity"] for item in items), 2)
    parsed = address_parser.city_and_street(address) if address else None
    city, street = parsed if parsed else ("", "")

    draft = OrderDraft(items=items, items_total=items_total, stage="awaiting_delivery")
    draft.details.update({"vk_order_id": order_id, "origin": "storefront"})
    if address:
        draft.details["vk_order_address"] = address
    if city:
        draft.details["storefront_city"] = city
        draft.details["storefront_street"] = street
    from app.modules.orders import repeat_delivery

    last = await repeat_delivery.last_recipient_for(peer_id)
    if recipient and not (last is not None and last.email):
        # Постоянному клиенту — прежний получатель кнопкой «Да, на эти данные»:
        # с почтой, которую уже проверяли.
        draft.details["storefront_recipient"] = {"name": recipient[0], "phone": recipient[1]}
    await state.set_draft(peer_id, draft)
    await analytics.ensure_client(peer_id)
    await funnel.record(peer_id, "draft_created", source_=funnel.STOREFRONT, origin="storefront",
                        vk_order_id=order_id)

    listed = ", ".join(f"{item['name']} × {item['quantity']}" for item in items)
    # Модель должна знать, о чём шаблонный вопрос клиента «как оплатить заказ».
    await dialog_history.append_message(
        peer_id, "assistant",
        f"Клиент оформил заказ №{order_id} в разделе «Товары» сообщества: {listed} — "
        f"{templates.amount(items_total)} ₽" + (f", доставка: {city or address}." if (city or address) else "."),
        author=dialog_history.AUTHOR_VK,
    )

    options = await _quote_both(draft, city, street) if city else []
    if options:
        await _offer_carriers(peer_id, order_id, items, items_total, city, options)
        await _tell_manager(order_id, peer_id, f"{listed} — {templates.amount(items_total)} ₽. "
                            "Клиенту предложены Ozon и СДЭК до его города.")
        return
    if address and await _direct_points(peer_id, order_id, {"recipient": {}} if not recipient else
                                        {"recipient": {"name": recipient[0], "phone": recipient[1]}},
                                        items, items_total, address):
        await _tell_manager(order_id, peer_id, f"{listed} — {templates.amount(items_total)} ₽. "
                            "Клиенту показаны пункты рядом с адресом из заказа.")
        return
    lines = [
        f"Заказ №{order_id} принят: {listed} — {templates.amount(items_total)} ₽.",
        "Осталось выбрать доставку. Дешевле всего пункт выдачи Ozon — "
        "заберёте сами. Быстрее, но дороже — пункт выдачи СДЭК, ещё есть "
        "курьер СДЭК до двери.",
        f"В какой город везём? Адрес из заказа — «{address}», везём туда?" if address else "В какой город везём?",
    ]
    # Через журнал отправок: одно событие — одно сообщение, и сказанное
    # ботом попадает в историю диалога.
    await client_messages.send(
        peer_id=peer_id, ref=f"vk_order:{order_id}", event_type=templates.STOREFRONT_ORDER,
        text="\n".join(lines),
    )
    await _tell_manager(order_id, peer_id, f"{listed} — {templates.amount(items_total)} ₽. "
                        "Клиенту предложено выбрать доставку в диалоге.")


async def _quote_both(draft: OrderDraft, city: str, street: str) -> list[dict]:
    """Ozon и СДЭК до города — параллельно; кто не посчитал, того не предлагаем."""
    from app.core.config import free_delivery_threshold
    from app.modules.delivery import ozon_quote
    from app.modules.orders import conversation, eta

    async def ozon() -> dict | None:
        if not ozon_quote.is_ready():
            return None
        picked = await conversation._ozon_points(draft, city, street)
        if not picked.points:
            return None
        quote = await conversation._ozon_price(draft, int(picked.points[0].id))
        return {"method": "ozon_pvz", "carrier": "Ozon", "cost": quote.total,
                "eta": {"carrier": "ozon", "min": quote.days, "max": quote.days, "working": False}}

    async def cdek() -> dict | None:
        tariff, total = await conversation._cdek_delivery(draft, "cdek_pvz", city)
        return {"method": "cdek_pvz", "carrier": "СДЭК", "cost": total,
                "eta": {"carrier": "cdek", "min": tariff.period_min, "max": tariff.period_max,
                        "working": True}}

    async def safe(job, name):
        try:
            return await asyncio.wait_for(job(), timeout=20)
        except Exception as error:
            logger.warning("Витрина: %s до города не посчитан — %s", name, type(error).__name__)
            return None

    found = [o for o in await asyncio.gather(safe(ozon, "Ozon"), safe(cdek, "СДЭК")) if o]
    free = bool(free_delivery_threshold()) and draft.items_total >= free_delivery_threshold()
    for option in found:
        option["client_cost"] = 0 if free else round(option["cost"], 2)
        option["eta_phrase"] = eta.phrase_for(option["eta"])
    return found


async def _offer_carriers(peer_id: int, order_id: int, items: list[dict], items_total: float,
                          city: str, options: list[dict]) -> None:
    from app.messages import keyboard as keyboards
    from app.modules.orders import buttons

    draft = await state.get_draft(peer_id)
    draft.details["storefront_quotes"] = [
        {key: option[key] for key in ("method", "carrier", "client_cost", "eta_phrase")} for option in options
    ]
    await state.set_draft(peer_id, draft)
    keyboard = buttons.carrier_keyboard(draft.details["version"], options)
    shows = await keyboards.for_peer(peer_id, keyboard) is not None
    await client_messages.send(
        peer_id=peer_id, ref=f"vk_order:{order_id}", event_type=templates.STOREFRONT_ORDER,
        text=templates.storefront_carriers(order_id=order_id, items=items, items_total=items_total,
                                           city=city, options=options, button=shows),
        keyboard=keyboard,
    )


async def after_carrier(peer_id: int, method: str) -> tuple[str, dict | None] | None:
    """Клиент выбрал перевозчика: пункты (или просьба об адресе пункта) и почта для чека."""
    from app.modules.orders import buttons, conversation, eta

    draft = await state.get_draft(peer_id)
    if draft is None:
        return None
    city = draft.details.get("storefront_city", "")
    street = draft.details.get("storefront_street", "")
    await conversation._execute_set_delivery_method(
        peer_id, {"method": method, "address": city, "pickup_point": street}
    )
    fresh = await state.get_draft(peer_id)
    if fresh is None or fresh.delivery_method != method:
        return None
    shown = fresh.details.get("shown_points") or []
    asked = bool(fresh.details.get("point_asked"))
    if not (shown or asked):
        return None
    carrier = "Ozon" if method == "ozon_pvz" else "СДЭК"
    from app.modules.orders import repeat_delivery

    keyboard, hint = await buttons.for_reply(peer_id)
    candidate = fresh.details.get("storefront_recipient")
    last = None if candidate else await repeat_delivery.last_recipient_for(peer_id)
    text = templates.storefront_carrier_chosen(
        carrier=carrier, city=city, delivery_cost=fresh.delivery_cost, eta=eta.phrase(fresh.details),
        shown=shown, asked=asked, recipient=candidate, hint=hint if keyboard else "",
        last=(last.name, last.phone, last.email) if last is not None and last.email else None,
        button=keyboard is not None,
    )
    return text, keyboard


async def offered_recently(order_id: int, minutes: int = 10) -> bool:
    """Предложение доставки по заказу ушло только что — шаблонный вопрос им и отвечен.

    Через десять минут тот же вопрос — уже вопрос, и отвечает модель.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.messages.models import ClientNotice

    async with get_session_factory()() as session:
        sent_at = (await session.execute(
            select(ClientNotice.sent_at).where(
                ClientNotice.ref == f"vk_order:{order_id}", ClientNotice.event_type == templates.STOREFRONT_ORDER,
            )
        )).scalar()
    return sent_at is not None and datetime.now(timezone.utc) - sent_at < timedelta(minutes=minutes)


def looks_like_order_question(text: str) -> bool:
    """Шаблон ВК, который клиент отправляет, открыв диалог после заказа в «Товарах»."""
    lowered = (text or "").casefold()
    return "как оплатить заказ" in lowered or ("оплатить" in lowered and "доставить" in lowered)


async def wait_for_order(peer_id: int, seconds: float = 15) -> OrderDraft | None:
    """Шаблонный вопрос пришёл раньше заказа — подождать, пока заказ разберётся.

    Сообщение клиента и уведомления о заказе приходят в одну секунду, а
    расчёт двух перевозчиков занимает несколько. Ответь модель сразу — она не
    знала бы про заказ, а следом пришло бы предложение доставки.
    """
    deadline = time.monotonic() + seconds
    while True:
        draft = await state.get_draft(peer_id)
        order_id = draft.details.get("vk_order_id") if draft else None
        if order_id and await client_messages.already_sent(f"vk_order:{order_id}", templates.STOREFRONT_ORDER):
            return draft
        if time.monotonic() >= deadline:
            return draft if order_id else None
        await asyncio.sleep(1)
